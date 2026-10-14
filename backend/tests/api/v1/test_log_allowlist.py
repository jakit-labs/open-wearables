"""No log line carries a request value (BH.Loop fork, T-CYK-1009 AC-5).

Each test drives one path a physiological value, an OAuth secret or a provider
token can take, with a synthetic sentinel in it, and reads back everything the
process writes through its own sinks: every stream handler the app configured,
plus anything printed to stdout or stderr. Not one line may hold the sentinel.
"""

import io
import json
import logging
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from celery import signals
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import DbSession, _get_db_dependency, async_engine, engine
from app.integrations.celery.tasks.process_sdk_upload_task import process_sdk_upload
from app.main import api, celery_app
from app.models import User
from app.services.providers.factory import ProviderFactory

SENTINEL = "173.917"  # a synthetic heart-rate-like value; never real data
STORED_SENTINEL = "173.92"  # the same value as a numeric(5,2) column keeps it


@pytest.fixture
def log_output(capsys: pytest.CaptureFixture[str]) -> Generator[Callable[[], str], None, None]:
    """Everything the process writes to stdout/stderr through its sinks, as one string."""
    buffer = io.StringIO()
    loggers = [logging.getLogger()] + [
        logger for logger in logging.Logger.manager.loggerDict.values() if isinstance(logger, logging.Logger)
    ]
    swapped: list[tuple[logging.StreamHandler, Any]] = []
    for logger in loggers:
        for handler in logger.handlers:
            # pytest's own capture handlers are not a sink of the app
            if isinstance(handler, logging.StreamHandler) and not type(handler).__module__.startswith("_pytest"):
                swapped.append((handler, handler.setStream(buffer)))

    def read() -> str:
        captured = capsys.readouterr()
        return buffer.getvalue() + captured.out + captured.err

    yield read
    for handler, stream in swapped:
        if stream is not None:
            handler.setStream(stream)


@pytest.fixture
def server_error_client(db: Session) -> Generator[TestClient, None, None]:
    """The real app, answering an unhandled error with a 500 instead of raising it."""

    def override_get_db() -> Generator[Session, None, None]:
        yield db

    api.dependency_overrides[_get_db_dependency] = override_get_db
    with TestClient(api, raise_server_exceptions=False) as client:
        yield client
    api.dependency_overrides.clear()


def _assert_no_sentinel(output: str) -> None:
    leaks = [line for line in output.splitlines() if SENTINEL in line or STORED_SENTINEL in line]
    assert leaks == [], f"{len(leaks)} log line(s) carry the sentinel: {leaks[:3]}"


class TestNoRequestValueReachesTheLogs:
    def test_400_validation_error(
        self, client: TestClient, api_key_header: dict[str, str], log_output: Callable[[], str]
    ) -> None:
        response = client.post("/api/v1/users", json={"email": f"{SENTINEL}@"}, headers=api_key_header)

        assert response.status_code == 400
        _assert_no_sentinel(log_output())

    def test_500_database_error(self, server_error_client: TestClient, log_output: Callable[[], str]) -> None:
        def failing_statement(db: DbSession) -> None:
            # a real statement error whose bound parameter is the value
            db.execute(text("SELECT CAST(:value AS integer)"), {"value": SENTINEL})

        api.add_api_route("/zz-log-probe/db-error", failing_statement, methods=["POST"])
        try:
            response = server_error_client.post("/zz-log-probe/db-error")
        finally:
            api.router.routes.pop()

        assert response.status_code == 500
        _assert_no_sentinel(log_output())

    def test_sdk_log_request_naming_its_own_provider(
        self, client: TestClient, user: User, api_key_header: dict[str, str], log_output: Callable[[], str]
    ) -> None:
        # ``provider`` is an allowed key, and here its value is request text
        response = client.post(
            f"/api/v1/sdk/users/{user.id}/logs",
            json={
                "sdkVersion": "1.0.0",
                "provider": f"watch-{SENTINEL}",
                "events": [{"eventType": "device_state", "timestamp": "2025-04-10T12:00:00Z", "batteryLevel": 0.5}],
            },
            headers=api_key_header,
        )

        assert response.status_code == 202
        _assert_no_sentinel(log_output())

    def test_oauth_callback_code_and_state(self, client: TestClient, log_output: Callable[[], str]) -> None:
        response = client.get(
            "/api/v1/oauth/oura/callback",
            params={"code": f"code-{SENTINEL}", "state": f"state-{SENTINEL}"},
            follow_redirects=False,
        )

        assert response.status_code == 400  # unknown state
        _assert_no_sentinel(log_output())

    def test_provider_token(self, db: Session, user: User, log_output: Callable[[], str]) -> None:
        oauth = ProviderFactory().get_provider("oura").oauth
        assert oauth is not None
        token_url = "https://api.ouraring.com/oauth/token"
        # the provider refuses the refresh token and echoes it back, as some do
        rejection = httpx.Response(
            400,
            text=json.dumps({"error": "invalid_grant", "detail": f"refresh token tok-{SENTINEL} revoked"}),
            request=httpx.Request("POST", token_url),
        )

        with patch("httpx.post", return_value=rejection), pytest.raises(HTTPException):
            oauth.refresh_access_token(db, user.id, f"tok-{SENTINEL}")

        _assert_no_sentinel(log_output())

    def test_celery_task_failure(self, log_output: Callable[[], str]) -> None:
        root, celery_logger = logging.getLogger(), logging.getLogger("celery")
        saved = (root.level, celery_logger.level, list(celery_logger.handlers), celery_logger.propagate)

        @celery_app.task(name="tests.zz_log_probe_failing_task")
        def failing_task(value: str) -> None:
            raise ValueError(f"cannot store {value}")

        try:
            # what a worker or beat process does at start
            signals.setup_logging.send(sender=None, loglevel=logging.WARNING, logfile=None, format="", colorize=False)
            result = failing_task.apply(args=[SENTINEL])
        finally:
            root.setLevel(saved[0])
            celery_logger.setLevel(saved[1])
            celery_logger.handlers[:] = saved[2]
            celery_logger.propagate = saved[3]

        assert result.state == "FAILURE"
        _assert_no_sentinel(log_output())

    @patch("app.integrations.celery.tasks.process_sdk_upload_task.SessionLocal")
    def test_workout_import(
        self, mock_session_local: MagicMock, db: Session, user: User, log_output: Callable[[], str]
    ) -> None:
        mock_session_local.return_value.__enter__ = MagicMock(return_value=db)
        mock_session_local.return_value.__exit__ = MagicMock(return_value=None)
        workout = {
            "id": str(uuid4()),
            "type": "walking",
            "startDate": "2025-03-25T17:27:00Z",
            "endDate": "2025-03-25T18:51:24Z",
            "source": {"name": "Synthetic Watch", "bundleIdentifier": "com.apple.health", "deviceModel": "Watch"},
            "values": [
                {"type": "duration", "unit": "s", "value": 5064},
                {"type": "averageHeartRate", "unit": "bpm", "value": float(SENTINEL)},
            ],
        }
        content = json.dumps(
            {
                "provider": "apple",
                "sdkVersion": "1.0.0",
                "syncTimestamp": "2025-04-10T12:00:00Z",
                "data": {"records": [], "sleep": [], "workouts": [workout]},
            }
        )

        result = process_sdk_upload(content, "application/json", str(user.id), "apple")

        assert result["status_code"] == 200
        _assert_no_sentinel(log_output())


def test_statement_errors_hide_their_parameters() -> None:
    # A statement error's text then never carries a bound value into any sink.
    assert engine.hide_parameters is True
    assert async_engine.sync_engine.hide_parameters is True
