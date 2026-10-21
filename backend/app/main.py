import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.access_audit import AccessAuditMiddleware
from app.api import head_router
from app.config import settings
from app.integrations.celery import create_celery
from app.integrations.sentry import init_sentry
from app.middlewares import add_access_log_middleware, add_cors_middleware
from app.services import raw_payload_storage
from app.services.outgoing_webhooks import svix as svix_service
from app.utils.exceptions import DatetimeParseError, handle_exception
from app.utils.structured_logging import configure_logging

# Every record leaves through the root handler's allowlist formatter, on stdout
# (T-CYK-1009 AC-5).
configure_logging()

# httpx/httpcore log one INFO line per outgoing request; at our request volume that is
# pure noise (event-type sync, provider calls, webhook delivery). Keep warnings and errors.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Configure uvicorn's loggers here, not at import: uvicorn's configure_logging() runs a
    # dictConfig that re-creates them and undoes import-time changes. Lifespan runs after it,
    # so add_access_log_middleware stays the single access-log source, and uvicorn's own
    # handler, which prints an unhandled request's exception text, gives way to the root
    # allowlist (T-CYK-1009 AC-5).
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    svix_service.register_event_types()
    yield


api = FastAPI(title=settings.api_name, version=version("open-wearables"), lifespan=_lifespan)
celery_app = create_celery()
init_sentry()
raw_payload_storage.configure(
    settings.raw_payload_storage,
    settings.raw_payload_max_size_bytes,
    s3_bucket=settings.raw_payload_bucket,
    s3_prefix=settings.raw_payload_s3_prefix,
    s3_endpoint_url=settings.raw_payload_s3_endpoint_url,
    fit_files_enabled=settings.store_fit_files,
    transport_enabled=settings.sdk_payload_s3_offload,
)

add_cors_middleware(api)
add_access_log_middleware(api)
# Last added is outermost: every request, refused or not, leaves an audit row (T-CYK-1009 AC-1).
api.add_middleware(AccessAuditMiddleware)

# Mount static files for provider icons
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    api.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@api.get("/")
async def root() -> dict[str, str]:
    return {"message": "Server is running!"}


@api.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # (FastAPI ≥ 0.130 rejects empty required str form fields before the handler runs)
    if request.url.path.endswith("/auth/login"):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Incorrect email or password"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise handle_exception(exc, "")


@api.exception_handler(DatetimeParseError)
async def datetime_parse_exception_handler(_: Request, exc: DatetimeParseError) -> None:
    raise handle_exception(exc, "")


api.include_router(head_router)
