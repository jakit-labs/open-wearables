"""Test hook for ``TestServedApiLogs``, loaded only when a test puts this folder on PYTHONPATH.

When the file named by ``SERVED_PROBE_TRIGGER`` appears, the process makes the call
uvicorn makes on an unhandled request exception and raises one Python warning, each
carrying a synthetic reading, then writes ``<trigger>.done``.
"""

import logging
import os
import threading
import time
import warnings
from pathlib import Path


def _emit_when_triggered(trigger: Path) -> None:
    while not trigger.exists():
        time.sleep(0.1)
    # uvicorn.protocols.http: logger.error("Exception in ASGI application\n", exc_info=exc)
    logging.getLogger("uvicorn.error").error(
        "Exception in ASGI application\n", exc_info=ValueError("heart rate 173.917 out of range")
    )
    warnings.warn("reading 164.281 rounded", UserWarning, stacklevel=1)
    Path(f"{trigger}.done").touch()


if trigger := os.environ.get("SERVED_PROBE_TRIGGER"):
    threading.Thread(target=_emit_when_triggered, args=(Path(trigger),), daemon=True).start()
