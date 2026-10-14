import logging
import ssl
from logging import LogRecord, getLogger
from typing import cast

from celery import Celery, signals
from celery import current_app as current_celery_app
from celery.schedules import crontab

from app.config import settings
from app.services import raw_payload_storage

_WEBHOOK_TASK = "emit_webhook_event_task.emit_webhook_event"


class _WebhookTraceFilter(logging.Filter):
    """Drop celery.app.trace success/retry records for the webhook emit task.

    Failures (ERROR and above) are always passed through.
    """

    def filter(self, record: LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            return True
        msg = record.getMessage()
        return _WEBHOOK_TASK not in msg


@signals.setup_logging.connect
def setup_celery_logging(loglevel: int | str = logging.WARNING, **kwargs) -> None:
    """Keep Celery on the root handler and its allowlist formatter (T-CYK-1009 AC-5).

    A connected receiver stops Celery from installing its own handlers and from
    redirecting stdout into a logger, so every worker and beat record leaves
    through the one sink app.main configured, at the level the command names.
    """
    getLogger().setLevel(loglevel)

    # celery.app.trace logs "Task ... succeeded in Xs: {result}" at INFO for
    # every task execution.  Suppress those lines only for the high-frequency
    # webhook emit task to avoid log spam while keeping traces for all others.
    getLogger("celery.app.trace").addFilter(_WebhookTraceFilter())


@signals.worker_init.connect
def init_raw_payload_storage(**kwargs) -> None:
    """Initialize raw payload storage in celery workers."""
    raw_payload_storage.configure(
        settings.raw_payload_storage,
        settings.raw_payload_max_size_bytes,
        s3_bucket=settings.raw_payload_bucket,
        s3_prefix=settings.raw_payload_s3_prefix,
        s3_endpoint_url=settings.raw_payload_s3_endpoint_url,
        fit_files_enabled=settings.store_fit_files,
        transport_enabled=settings.sdk_payload_s3_offload,
    )


def create_celery() -> Celery:
    celery_app = cast(Celery, current_celery_app)
    celery_app.conf.update(
        broker_url=settings.redis_url,
        result_backend=settings.redis_url,
        # Detect and drop half-open TCP connections to Redis. Behind a NAT (e.g. Docker ->
        # ElastiCache) the conntrack entry can expire and the server-side RST never reaches
        # the worker, so it hangs forever on a dead socket and stops consuming — which is
        # what let the queue grow until Redis hit maxmemory. Keepalive probes surface the
        # dead socket so the connection is recycled instead of blocking indefinitely.
        broker_transport_options={
            "socket_keepalive": True,
            # Linux TCP socket-option ints: 4=TCP_KEEPIDLE (60s idle before probing),
            # 5=TCP_KEEPINTVL (10s between probes), 6=TCP_KEEPCNT (3 failed probes -> drop).
            "socket_keepalive_options": {4: 60, 5: 10, 6: 3},
            # redis-py periodic health check: PING idle pooled connections so a dead one is
            # replaced rather than handed to the next publish/consume.
            "health_check_interval": 30,
        },
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        task_default_queue="default",
        task_default_exchange="default",
        result_expires=3 * 24 * 3600,
        control_queue_ttl=300,
        control_queue_expires=300,
        task_queues={
            "default": {},
            "sdk_sync": {},
            "garmin_sync": {},
            "webhook_sync": {},
            "xml_sync": {},
        },
        task_routes={
            "app.integrations.celery.tasks.process_sdk_upload_task.process_sdk_upload": {"queue": "sdk_sync"},
            "app.integrations.celery.tasks.process_aws_upload_task.process_aws_upload": {"queue": "xml_sync"},
            "app.integrations.celery.tasks.process_aws_upload_task.complete_and_process_aws_upload": {
                "queue": "xml_sync"
            },
            "app.integrations.celery.tasks.process_xml_upload_task.process_xml_upload": {"queue": "xml_sync"},
        },
    )

    # rediss:// alone isn't enough for Celery — the broker/result transports read
    # their TLS requirements from these dicts. Required for ElastiCache (TLS).
    if settings.redis_ssl:
        ssl_options = {"ssl_cert_reqs": ssl.CERT_REQUIRED}
        celery_app.conf.broker_use_ssl = ssl_options
        celery_app.conf.redis_backend_use_ssl = ssl_options

    celery_app.autodiscover_tasks(["app.integrations.celery.tasks", "app.integrations.celery.tasks.garmin"])

    celery_app.conf.beat_schedule = {
        "sync-all-users-periodic": {
            "task": "app.integrations.celery.tasks.periodic_sync_task.sync_all_users",
            "schedule": float(settings.sync_interval_seconds),
            "args": (),  # No args - task calculates date range dynamically
            "kwargs": {"user_id": None},
        },
        "finalize-stale-sleeps-periodic": {
            "task": "app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps",
            "schedule": float(settings.sleep_sync_interval_seconds),
            "args": (),
            "kwargs": {},
        },
        "run-daily-archival": {
            "task": "app.integrations.celery.tasks.archival_task.run_daily_archival",
            "schedule": crontab(hour=3, minute=0),  # Daily at 03:00 UTC
            "args": (),
            "kwargs": {},
        },
        "fill-missing-sleep-scores": {
            "task": "app.integrations.celery.tasks.fill_missing_sleep_scores_task.fill_missing_sleep_scores",
            "schedule": float(settings.sleep_score_interval_seconds),
            "args": (),
            "kwargs": {},
        },
        "fill-missing-resilience-scores": {
            "task": "app.integrations.celery.tasks.fill_missing_resilience_scores_task.fill_missing_resilience_scores",
            "schedule": float(settings.resilience_score_interval_seconds),
            "args": (),
            "kwargs": {},
        },
        "close-stale-sync-runs": {
            "task": "app.integrations.celery.tasks.close_stale_sync_runs_task.close_stale_sync_runs",
            "schedule": float(settings.sync_run_sweep_interval_seconds),
            "args": (),
            "kwargs": {},
        },
        "renew-oura-webhooks-monthly": {
            "task": "app.integrations.celery.tasks.renew_oura_webhooks_task.renew_oura_webhooks",
            "schedule": crontab(day_of_month=1, hour=0, minute=0),  # 1st of each month at 00:00 UTC
            "args": (),
            "kwargs": {},
        },
    }

    return celery_app
