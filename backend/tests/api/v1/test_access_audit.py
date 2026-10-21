"""The access audit: one row for every request the sidecar serves, denials included (BH.Loop fork, T-CYK-1009 AC-1).

These tests drive the real app through the in-process client. The rows land in the
test database on the audit's own connection, outside the test's rolled-back
transaction, so each test reads what a real request leaves behind. The runtime
role's INSERT-only grant is proven on the served app in ``test_runtime_login.py``.
"""

import re
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from app import access_audit
from app.access_audit import POLICY_VERSION, AccessAuditMiddleware
from app.main import api
from app.models import AccessAudit, ApiKey, User

TENANT = "9d1f6a43-1c55-4e4a-9a0e-2d5a3c7b1e01"
ACTOR = "5b0c2e7a-8f3d-4b61-a2c9-7e4d1f0a3b02"
OTHER = "0e8f4d21-6a7b-4c3d-8e9f-1a2b3c4d5e6f"
CONTEXT_FIELDS = ("actor_ref", "effective_role", "tenant_id", "purpose")

Rows = Callable[[], list[dict[str, Any]]]


def context(**overrides: str) -> dict[str, str]:
    """The four context headers BH.Loop's API sends on an API-key call."""
    headers = {
        "x-bh-loop-tenant": TENANT,
        "x-bh-loop-actor": ACTOR,
        "x-bh-loop-role": "staff",
        "x-bh-loop-purpose": "wellness_operation",
    }
    headers.update(overrides)
    return headers


@pytest.fixture
def rows(engine: Engine) -> Rows:
    """Reads every audit row, attempts before outcomes, after clearing the table."""
    with engine.begin() as connection:
        connection.execute(delete(AccessAudit))

    def read() -> list[dict[str, Any]]:
        with engine.connect() as connection:
            found = [dict(row._mapping) for row in connection.execute(select(AccessAudit))]
        return sorted(found, key=lambda row: (row["created_at"], row["outcome"] != "attempt"))

    return read


def key_and(api_key: ApiKey, headers: dict[str, str]) -> dict[str, str]:
    return {"X-Open-Wearables-API-Key": api_key.plain_key, **headers}


class TestEveryRequestLeavesARow:
    def test_a_read_writes_one_row_naming_every_field(
        self, client: TestClient, api_key: ApiKey, user: User, rows: Rows
    ) -> None:
        response = client.get(f"/api/v1/users/{user.id}", headers=key_and(api_key, context()))

        assert response.status_code == 200
        [row] = rows()
        assert row["actor_ref"] == UUID(ACTOR)
        assert row["actor_kind"] == "staff"
        assert row["effective_role"] == "staff"
        assert row["tenant_id"] == UUID(TENANT)
        assert row["purpose"] == "wellness_operation"
        assert row["target_ref"] == user.id
        assert row["policy_version"] == POLICY_VERSION
        assert (row["method"], row["route"], row["status"], row["outcome"]) == (
            "GET",
            "/api/v1/users/{user_id}",
            200,
            "allowed",
        )
        assert row["created_at"] is not None

    def test_a_mutation_writes_its_attempt_before_its_outcome(
        self, client: TestClient, api_key: ApiKey, rows: Rows
    ) -> None:
        response = client.post("/api/v1/users", headers=key_and(api_key, context()), json={})

        assert response.status_code == 201
        attempt, outcome = rows()
        assert (attempt["outcome"], attempt["status"]) == ("attempt", None)
        assert (outcome["outcome"], outcome["status"]) == ("allowed", 201)
        assert attempt["request_id"] == outcome["request_id"]
        assert attempt["id"] != outcome["id"]
        for row in (attempt, outcome):
            assert row["route"] == "/api/v1/users"
            assert (row["tenant_id"], row["actor_ref"]) == (UUID(TENANT), UUID(ACTOR))

    def test_a_service_call_names_the_service(
        self, client: TestClient, api_key: ApiKey, user: User, rows: Rows
    ) -> None:
        headers = key_and(api_key, context(**{"x-bh-loop-role": "system_job"}))

        assert client.get(f"/api/v1/users/{user.id}", headers=headers).status_code == 200
        [row] = rows()
        assert (row["actor_kind"], row["effective_role"]) == ("service", "system_job")

    def test_the_authorize_call_names_its_user_from_the_query(
        self, client: TestClient, api_key: ApiKey, user: User, rows: Rows
    ) -> None:
        client.get(f"/api/v1/oauth/oura/authorize?user_id={user.id}", headers=key_and(api_key, context()))

        [row] = rows()
        assert (row["route"], row["target_ref"]) == ("/api/v1/oauth/{provider}/authorize", user.id)


class TestADenialLeavesARow:
    def test_a_bad_api_key(self, client: TestClient, rows: Rows) -> None:
        response = client.get("/api/v1/users", headers={"X-Open-Wearables-API-Key": "sk-not-a-key"})

        assert response.status_code == 401
        [row] = rows()
        assert (row["outcome"], row["status"], row["route"], row["actor_kind"]) == (
            "denied",
            401,
            "/api/v1/users",
            "anonymous",
        )
        assert [row[field] for field in CONTEXT_FIELDS] == [None] * 4

    def test_an_unmatched_path(self, client: TestClient, rows: Rows) -> None:
        assert client.get("/api/v1/no-such-route").status_code == 404

        [row] = rows()
        assert (row["outcome"], row["status"], row["route"], row["target_ref"]) == ("denied", 404, None, None)

    def test_a_wrong_method(self, client: TestClient, user: User, rows: Rows) -> None:
        assert client.put(f"/api/v1/users/{user.id}", json={}).status_code == 405

        attempt, outcome = rows()
        assert attempt["outcome"] == "attempt"
        assert (outcome["outcome"], outcome["status"], outcome["route"]) == ("denied", 405, "/api/v1/users/{user_id}")

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("x-bh-loop-tenant", "not-a-uuid"),
            ("x-bh-loop-tenant", TENANT.upper()),
            ("x-bh-loop-actor", "someone@example.com"),
            ("x-bh-loop-role", "superuser"),
            ("x-bh-loop-purpose", "marketing"),
        ],
    )
    def test_an_invalid_context_value_is_refused_and_never_stored(
        self, client: TestClient, api_key: ApiKey, db: Session, rows: Rows, name: str, value: str
    ) -> None:
        response = client.post("/api/v1/users", headers=key_and(api_key, context(**{name: value})), json={})

        assert (response.status_code, response.content) == (403, b"")
        assert db.query(User).count() == 0  # the app was never reached
        [row] = rows()
        assert (row["outcome"], row["status"], row["actor_kind"]) == ("denied", 403, "anonymous")
        assert [row[field] for field in CONTEXT_FIELDS] == [None] * 4
        assert value not in repr(row)

    def test_a_partial_context_is_refused(self, client: TestClient, api_key: ApiKey, rows: Rows) -> None:
        headers = key_and(api_key, context())
        del headers["x-bh-loop-purpose"]

        assert client.get("/api/v1/users", headers=headers).status_code == 403
        [row] = rows()
        assert (row["outcome"], row["tenant_id"]) == ("denied", None)

    def test_a_repeated_context_header_is_refused(self, client: TestClient, api_key: ApiKey, rows: Rows) -> None:
        headers = [*key_and(api_key, context()).items(), ("x-bh-loop-tenant", OTHER)]

        assert client.get("/api/v1/users", headers=headers).status_code == 403
        [row] = rows()
        assert (row["outcome"], row["tenant_id"]) == ("denied", None)


class TestPublicPaths:
    """BH.Loop relays these with no context headers; the route names the caller."""

    def test_a_provider_webhook(self, client: TestClient, rows: Rows) -> None:
        client.post("/api/v1/providers/oura/webhooks", content=b'{"reading": 173.917}')

        attempt, outcome = rows()
        for row in (attempt, outcome):
            assert (row["actor_kind"], row["route"]) == ("provider", "/api/v1/providers/{provider}/webhooks")
        assert outcome["outcome"] == "denied"  # no signature

    def test_an_oauth_callback_stores_no_code_or_state(self, client: TestClient, rows: Rows) -> None:
        client.get("/api/v1/oauth/oura/callback?code=sentinel-code-173917&state=sentinel-state-173917")

        [row] = rows()
        assert (row["actor_kind"], row["route"]) == ("provider", "/api/v1/oauth/{provider}/callback")
        assert "173917" not in repr(row)

    def test_an_sdk_sync(self, client: TestClient, user: User, rows: Rows) -> None:
        client.post(f"/api/v1/sdk/users/{user.id}/sync", json={})

        attempt, outcome = rows()
        assert (attempt["actor_kind"], outcome["actor_kind"]) == ("sdk", "sdk")
        assert outcome["target_ref"] == user.id
        assert (outcome["outcome"], outcome["status"]) == ("denied", 401)


class TestNoRowNoAccess:
    """D11 allows no sampling: a request whose row cannot be written is not served."""

    def test_a_read_whose_row_fails_answers_503_with_no_body(
        self, client: TestClient, api_key: ApiKey, user: User
    ) -> None:
        with patch.object(access_audit, "_insert", side_effect=OSError("audit store down")):
            response = client.get(f"/api/v1/users/{user.id}", headers=key_and(api_key, context()))

        assert (response.status_code, response.content) == (503, b"")

    def test_a_mutation_whose_attempt_fails_never_runs(self, client: TestClient, api_key: ApiKey, db: Session) -> None:
        with patch.object(access_audit, "_insert", side_effect=OSError("audit store down")):
            response = client.post("/api/v1/users", headers=key_and(api_key, context()), json={})

        assert (response.status_code, response.content) == (503, b"")
        assert db.query(User).count() == 0

    def test_a_mutation_whose_outcome_fails_keeps_its_attempt(
        self, client: TestClient, api_key: ApiKey, rows: Rows
    ) -> None:
        real = access_audit._insert
        calls: list[str] = []

        def attempt_then_fail(row: dict[str, Any]) -> None:
            calls.append(row["outcome"])
            if len(calls) > 1:
                raise OSError("audit store down")
            real(row)

        with patch.object(access_audit, "_insert", side_effect=attempt_then_fail):
            response = client.post("/api/v1/users", headers=key_and(api_key, context()), json={})

        assert (response.status_code, response.content) == (503, b"")
        assert calls == ["attempt", "allowed"]
        [row] = rows()
        assert row["outcome"] == "attempt"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/boom/{user_id}")
    async def boom(user_id: str) -> None:
        raise RuntimeError(f"kaboom {user_id}")

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        def body() -> Iterator[bytes]:
            # the row is already stored when the first byte is produced
            yield f"{len(_read_all())}".encode()

        return StreamingResponse(body())

    app.add_middleware(AccessAuditMiddleware)
    return app


def _read_all() -> list[Any]:
    with access_audit.engine.connect() as connection:
        return list(connection.execute(select(AccessAudit)))


class TestTheMiddleware:
    def test_an_unhandled_error_is_an_error_row_and_is_raised(self, rows: Rows) -> None:
        user_id = "17391700-0000-4000-8000-000000173917"

        with pytest.raises(RuntimeError), TestClient(_app()) as client:
            client.get(f"/boom/{user_id}")

        [row] = rows()
        assert (row["outcome"], row["status"], row["route"], row["target_ref"]) == (
            "error",
            500,
            "/boom/{user_id}",
            UUID(user_id),
        )

    def test_a_stream_has_its_row_when_it_starts(self, rows: Rows) -> None:
        with TestClient(_app()) as client:
            response = client.get("/stream")

        assert response.text == "1"
        [row] = rows()
        assert (row["route"], row["status"], row["outcome"]) == ("/stream", 200, "allowed")

    def test_every_route_of_the_app_resolves_to_its_own_template(self) -> None:
        """The audit names the route before the app runs; no route of ours shadows another."""
        for route in iter_route_contexts(api.routes):
            if not route.methods:
                continue
            path = re.sub(r"\{[^}]+\}", OTHER, route.path)
            assert access_audit.resolve_route(api, path, next(iter(route.methods)))[0] == route.path, path
