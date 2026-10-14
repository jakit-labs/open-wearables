"""Tests for access-log level derivation and the access-log middleware."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import Settings, settings
from app.middlewares import add_access_log_middleware
from app.utils.config_utils import AccessLogLevel, EnvironmentType


class TestAccessLogLevelDerivation:
    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            (EnvironmentType.PRODUCTION, AccessLogLevel.ERRORS),
            (EnvironmentType.STAGING, AccessLogLevel.ALL),
            (EnvironmentType.LOCAL, AccessLogLevel.ALL),
            (EnvironmentType.TEST, AccessLogLevel.ALL),
        ],
    )
    def test_default_is_derived_from_environment(self, environment: EnvironmentType, expected: AccessLogLevel) -> None:
        assert Settings(environment=environment, access_log_level=None).access_log_level == expected

    def test_explicit_value_overrides_derivation(self) -> None:
        settings = Settings(environment=EnvironmentType.PRODUCTION, access_log_level=AccessLogLevel.ALL)
        assert settings.access_log_level == AccessLogLevel.ALL


def _build_client(level: AccessLogLevel) -> TestClient:
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/client-error")
    async def client_error() -> None:
        raise HTTPException(status_code=404)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    with patch.object(settings, "access_log_level", level):
        add_access_log_middleware(app)

    return TestClient(app, raise_server_exceptions=False)


def _logged_statuses(mock: MagicMock) -> list[int]:
    return [call.kwargs["status"] for call in mock.call_args_list]


class TestAccessLogMiddleware:
    def test_all_level_logs_every_status(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/ok")
            client.get("/client-error")
            client.get("/boom")
        assert _logged_statuses(mock) == [200, 404, 500]

    def test_errors_level_drops_2xx_keeps_4xx_5xx(self) -> None:
        client = _build_client(AccessLogLevel.ERRORS)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/ok")
            client.get("/client-error")
            client.get("/boom")
        assert _logged_statuses(mock) == [404, 500]

    def test_off_level_logs_nothing(self) -> None:
        client = _build_client(AccessLogLevel.OFF)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/ok")
            client.get("/client-error")
        mock.assert_not_called()

    def test_log_level_is_error_for_4xx_and_5xx(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/ok")
            client.get("/client-error")
        levels = [call.args[1] for call in mock.call_args_list]
        assert levels == ["info", "error"]

    def test_route_template_replaces_the_raw_path(self) -> None:
        # T-CYK-1009 AC-5: the raw path and query carry ids and secrets; only the
        # matched route's template is logged.
        app = FastAPI()

        @app.get("/users/{user_id}")
        async def user(user_id: str) -> dict[str, str]:
            return {"user_id": user_id}

        with patch.object(settings, "access_log_level", AccessLogLevel.ALL):
            add_access_log_middleware(app)
        client = TestClient(app)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/users/s3cret-id?token=s3cret&appli=1")
            client.get("/no-such-route/s3cret-id")
        assert [call.kwargs["route"] for call in mock.call_args_list] == ["/users/{user_id}", None]
        assert "s3cret" not in repr(mock.call_args_list)

    def test_a_500_logs_the_error_type_only(self) -> None:
        client = _build_client(AccessLogLevel.ALL)
        with patch("app.middlewares.log_structured") as mock:
            client.get("/boom")
        assert mock.call_args_list[0].kwargs["error_type"] == "RuntimeError"
        assert "kaboom" not in repr(mock.call_args_list)
