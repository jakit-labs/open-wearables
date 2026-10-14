"""Structured logging: one JSON line per record, allowlisted fields only.

Every log record of the process, ours and the libraries', leaves through one
handler on the root logger (``configure_logging``). Its formatter is the sink
allowlist of the BH.Loop fork (T-CYK-1009 AC-5): a line carries the time, the
level, the logger name, the event and the keys in ``ALLOWED_KEYS``, and nothing
else. The event is the record's message only when that message is a string
literal of the code that logged it, never interpolated with its arguments. So
no exception text, traceback, payload field, id or f-string value reaches the
output, whatever a caller passes.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from logging import Logger, LogRecord
from typing import Any, NamedTuple
from uuid import UUID

from app.schemas.enums.provider import ProviderName


class LogContext(NamedTuple):
    """Per-request log fields threaded into downstream fetch/save logs.

    Lets callers pass the provider-side user id and trace id as a single
    argument instead of threading two separate params through every signature.
    Unpack at the top of a method:  ``provider_user_id, trace_id = log_ctx or LogContext()``.
    """

    provider_user_id: str | None = None
    trace_id: str | None = None


def json_serial(obj: Any) -> str:
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


# The only caller keys a line may carry (T-CYK-1009 AC-5). A caller's other
# keys (user ids, provider ids, error text, payload fields) are dropped here.
ALLOWED_KEYS = frozenset({"method", "route", "status", "duration_ms", "error_type", "provider", "task", "outcome"})
# Request text can reach ``provider`` (an SDK request names its own), so it passes
# only as a known provider name. The other allowed keys carry code-defined values.
_PROVIDERS = frozenset(provider.value for provider in ProviderName)


def _allowed(key: str, value: Any) -> bool:
    if key not in ALLOWED_KEYS or value is None:
        return False
    return key != "provider" or getattr(value, "value", value) in _PROVIDERS


def _literal_event(record: LogRecord) -> str | None:
    """The record's message if it is a string literal of the logging code, else None.

    The formatter runs inside the logging call, so the caller's frame is on the
    stack at the record's file and line. A message built at run time (an
    f-string, a str() of an exception) is not one of that code's constants.
    """
    frame = sys._getframe()
    while frame is not None:
        code = frame.f_code
        if code.co_filename == record.pathname and frame.f_lineno == record.lineno:
            if isinstance(record.msg, str) and record.msg in code.co_consts:
                return record.msg
            return None
        frame = frame.f_back
    return None


class AllowlistFormatter(logging.Formatter):
    """Format a record as one JSON line of allowlisted fields."""

    def format(self, record: LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": _literal_event(record),
        }
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            entry.update({key: value for key, value in structured.items() if _allowed(key, value)})
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Route every record to stdout through the allowlist formatter.

    stdout, not stderr: some platforms turn every stderr line into an error.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AllowlistFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def log_structured(
    logger: Logger,
    level: str,
    message: str,
    provider: str | None = None,
    **attributes: Any,
) -> None:
    """Log one structured record through ``logger``.

    The root handler keeps the message only if it is a literal at the call site,
    plus ``provider`` and any ``ALLOWED_KEYS`` among ``attributes``; every other
    attribute is dropped at the sink.

    Example:
        log_structured(logger, "info", "Apple sync batch received", provider="apple", task="sdk_sync")
    """
    logger.log(
        logging.getLevelNamesMapping()[level.upper()],
        message,
        extra={"structured": {"provider": provider, **attributes}},
        stacklevel=2,
    )
