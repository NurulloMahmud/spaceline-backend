"""Fire and forget: hub calls run after the commit, in a thread, and never raise."""

import logging
import threading
from typing import Any, Callable

from django.db import transaction

logger = logging.getLogger(__name__)


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - the caller is long gone
        logger.exception("dispatch background call %s failed", getattr(fn, "__name__", fn))


def _start(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="dispatch-hub", daemon=True).start()


def run_in_background(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    _start(lambda: _safe(fn, *args, **kwargs))


def after_commit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run fn(*args) in a background thread once the current transaction commits
    (at once outside a transaction)."""
    transaction.on_commit(lambda: run_in_background(fn, *args, **kwargs))
