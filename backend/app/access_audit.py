"""The access audit: one row for every request the sidecar serves, denials included.

BH.Loop fork (T-CYK-1009 AC-1). BH.Loop's API names who is asking on an API-key call
in four context headers: tenant, actor, effective role and purpose. The context is
all four headers or none, each sent once, every value on an allowlist. Anything else
is a denial: 403 with no body and a row whose four context fields are empty, so a
raw header value is never stored or logged. A call with no context (a provider
webhook, an OAuth callback, an SDK sync) is named by its route.

``AccessAuditMiddleware`` is the outermost middleware, so it sees unmatched paths,
405s, preflights and handler errors. It writes each row on its own connection
before the response leaves; a mutation also writes an ``attempt`` row before the
app runs. A row that cannot be written stops the request with 503: no access goes
unrecorded.
"""

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from sqlalchemy import insert
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database import engine
from app.models import AccessAudit
from app.utils.structured_logging import log_structured

logger = logging.getLogger("app.access_audit")

# Bumped with every change to what a row records or how the context is checked.
POLICY_VERSION = "bh-loop-access-audit/1"

# BH.Loop's PhiActorRoleSchema and PhiAccessPurposeSchema (packages/core/src/safety-flag-outcome.ts).
ROLES = frozenset({"member", "staff", "org_admin", "operator", "clinical_reviewer", "system_job"})
PURPOSES = frozenset(
    {
        "wellness_operation",
        "member_self_service",
        "lab_import",
        "clinical_review",
        "support",
        "deletion",
        "billing_reconciliation",
        "catalog_publication",
        "de_identification",
        "system_projection",
        "export",
    }
)
CONTEXT_HEADERS = (b"x-bh-loop-tenant", b"x-bh-loop-actor", b"x-bh-loop-role", b"x-bh-loop-purpose")

MUTATIONS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ACTOR_KIND_BY_ROLE = {"member": "member", "system_job": "service"}
_PUBLIC_ACTOR_KIND = {
    "/api/v1/providers/{provider}/webhooks": "provider",
    "/api/v1/oauth/{provider}/callback": "provider",
    "/api/v1/sdk/users/{user_id}/sync": "sdk",
}
_NO_CONTEXT: dict[str, Any] = {"tenant_id": None, "actor_ref": None, "effective_role": None, "purpose": None}


def resolve_route(app: FastAPI, path: str, method: str) -> tuple[str | None, dict[str, str]]:
    """The template of the route ``app`` will match for this request, and its path params.

    The middleware runs before the router, and FastAPI nests included routers, so a
    matched route's own ``path`` is relative to its router. This walks the effective
    routes in the router's order: the first path and method match, else the first
    path match (a 405), else no route.
    """
    allowed_elsewhere: tuple[str | None, dict[str, str]] | None = None
    for route in iter_route_contexts(app.routes):
        match = route.path_regex.match(path)
        if match is None:
            continue
        if not route.methods or method in route.methods:
            return route.path, match.groupdict()
        allowed_elsewhere = allowed_elsewhere or (route.path, match.groupdict())
    return allowed_elsewhere or (None, {})


def _canonical_uuid(value: str) -> UUID | None:
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if str(parsed) == value else None


def _read_context(headers: list[tuple[bytes, bytes]]) -> dict[str, Any] | None:
    """The four context fields, all None when none was sent; None when the context is invalid."""
    sent: dict[bytes, list[str]] = {}
    for name, value in headers:
        if name in CONTEXT_HEADERS:
            sent.setdefault(name, []).append(value.decode("latin-1"))
    if not sent:
        return dict(_NO_CONTEXT)
    if len(sent) != len(CONTEXT_HEADERS) or any(len(values) != 1 for values in sent.values()):
        return None
    tenant, actor, role, purpose = (sent[name][0] for name in CONTEXT_HEADERS)
    context = {
        "tenant_id": _canonical_uuid(tenant),
        "actor_ref": _canonical_uuid(actor),
        "effective_role": role if role in ROLES else None,
        "purpose": purpose if purpose in PURPOSES else None,
    }
    return context if None not in context.values() else None


def _actor_kind(context: dict[str, Any] | None, route: str | None) -> str:
    """Who is asking: the context's role on an API-key call, else what the route serves."""
    role = context["effective_role"] if context else None
    if role is None:
        return _PUBLIC_ACTOR_KIND.get(route, "anonymous")
    return _ACTOR_KIND_BY_ROLE.get(role, "staff")


def _target_ref(params: dict[str, str], query_string: bytes) -> UUID | None:
    """The sidecar user the request names, in its path or (the authorize call) its query."""
    value = params.get("user_id") or QueryParams(query_string).get("user_id")
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def _outcome(status: int) -> str:
    if status < 400:
        return "allowed"
    return "denied" if status < 500 else "error"


def _insert(row: dict[str, Any]) -> None:
    # No RETURNING: the runtime login may insert audit rows and never read them.
    with engine.begin() as connection:
        connection.execute(insert(AccessAudit).values(**row))


async def _write(row: dict[str, Any], outcome: str, status: int | None) -> bool:
    try:
        await run_in_threadpool(_insert, {**row, "id": uuid4(), "outcome": outcome, "status": status})
    except Exception as exc:
        log_structured(logger, "error", "access_audit_write_failed", error_type=type(exc).__name__)
        return False
    return True


async def _answer(send: Send, status: int) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


class AccessAuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        route, params = resolve_route(scope["app"], scope["path"], scope["method"])
        context = _read_context(scope["headers"])
        row = {
            **(context or _NO_CONTEXT),
            "request_id": uuid4(),
            "actor_kind": _actor_kind(context, route),
            "target_ref": _target_ref(params, scope["query_string"]),
            "policy_version": POLICY_VERSION,
            "method": scope["method"],
            "route": route,
        }
        if context is None:
            await _answer(send, 403 if await _write(row, "denied", 403) else 503)
            return
        if scope["method"] in MUTATIONS and not await _write(row, "attempt", None):
            await _answer(send, 503)
            return
        await self._serve(row, scope, receive, send)

    async def _serve(self, row: dict[str, Any], scope: Scope, receive: Receive, send: Send) -> None:
        """Run the app; its outcome row is written before its first byte leaves."""
        started = refused = False

        async def audited_send(message: Message) -> None:
            nonlocal started, refused
            if refused:
                return
            if message["type"] == "http.response.start":
                started = True
                if not await _write(row, _outcome(message["status"]), message["status"]):
                    refused = True
                    await _answer(send, 503)
                    return
            await send(message)

        try:
            await self.app(scope, receive, audited_send)
        except Exception:
            if not started:
                await _write(row, "error", 500)
            raise
