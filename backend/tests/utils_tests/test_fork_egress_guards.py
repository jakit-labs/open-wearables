"""Two outputs upstream added stay shut in the BH.Loop fork (T-CYK-1009).

- Telemetry: with default settings nothing goes to the telemetry endpoint from the API,
  the worker or beat.
- OTLP log export: it would ship log attributes past the log allowlist (AC-5), so
  OTEL_ENABLED=true stops a process at startup, and by default no export handler exists.
"""

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, settings
from app.constants.celery_tasks import CeleryTask
from app.integrations import otel
from app.integrations.celery.core import create_celery, enqueue_startup_telemetry_ping
from app.main import api
from app.services.telemetry_service import telemetry_service
from app.utils.logging_setup import configure_logging

BACKEND_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def default_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[bool]:
    """The app's settings with TELEMETRY_ENABLED left at the code default."""
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    default = Settings().telemetry_enabled
    with patch.object(settings, "telemetry_enabled", default):
        yield default


@pytest.fixture
def telemetry_posts() -> Iterator[MagicMock]:
    """Every httpx.post the process makes, so a test can look for the telemetry endpoint."""
    with patch("httpx.post") as post:
        yield post


def _to_telemetry(post: MagicMock) -> list[object]:
    return [call for call in post.call_args_list if settings.telemetry_endpoint_url in str(call)]


class TestTelemetryIsOffByDefault:
    def test_the_default_is_off(self, default_telemetry: bool) -> None:
        assert default_telemetry is False

    def test_the_api_sends_nothing(self, default_telemetry: bool, telemetry_posts: MagicMock, db: Session) -> None:
        with TestClient(api) as client:
            client.get("/")

        assert telemetry_service.send_ping(db, event="startup") == "disabled"
        assert _to_telemetry(telemetry_posts) == []

    def test_the_worker_sends_nothing(self, default_telemetry: bool, telemetry_posts: MagicMock, db: Session) -> None:
        worker = MagicMock()
        enqueue_startup_telemetry_ping(sender=worker)

        worker.app.send_task.assert_not_called()
        assert telemetry_service.send_ping(db, event="daily") == "disabled"
        assert _to_telemetry(telemetry_posts) == []

    def test_beat_schedules_no_ping(self, default_telemetry: bool) -> None:
        schedule = create_celery().conf.beat_schedule

        assert CeleryTask.SEND_TELEMETRY_PING not in {entry["task"] for entry in schedule.values()}


class TestOtelExportIsRefused:
    def test_otel_enabled_stops_the_process_and_prints_no_value(self) -> None:
        env = {**os.environ, "OTEL_ENABLED": "True"}
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "OTEL_ENABLED is refused in this fork" in output
        assert "input_value" not in output
        assert "'True'" not in output

    def test_settings_refuse_it_without_echoing_it(self) -> None:
        with pytest.raises(ValidationError) as refused:
            Settings(otel_enabled=True)

        assert "OTEL_ENABLED" in str(refused.value)
        assert "input_value" not in str(refused.value)

    def test_by_default_no_export_handler_is_attached(self) -> None:
        assert Settings().otel_enabled is False
        configure_logging()
        otel.init_otel("open-wearables-test")

        loggers = [logging.getLogger(name) for name in ("", "app", "celery", "uvicorn", "uvicorn.error")]
        assert otel.export_enabled() is False
        assert [handler for logger in loggers for handler in logger.handlers if otel.is_export_handler(handler)] == []
        assert [type(handler).__module__ for logger in loggers for handler in logger.handlers
                if type(handler).__module__.startswith("opentelemetry")] == []  # fmt: skip
