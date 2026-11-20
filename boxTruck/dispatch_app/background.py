"""Fire and forget: hub calls run after the commit, in a thread, and never raise.

The threads are not daemons, so a short-lived process (a management command,
a shell that turns a user off) waits for the call at exit instead of dropping
it. Each call carries the hub client's short timeout (3 s notify, 5 s status and
unlink), so that wait is bounded, and a request never waits on the hub at all:
the response goes out first, the thread runs beside it.
"""

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


def _start(target: Callable[[], None]) -> threading.Thread | None:
    thread = threading.Thread(target=target, name="dispatch-hub", daemon=False)
    thread.start()
    return thread


def run_in_background(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> threading.Thread | None:
    return _start(lambda: _safe(fn, *args, **kwargs))


def after_commit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run fn(*args) in a background thread once the current transaction commits
    (at once outside a transaction)."""
    transaction.on_commit(lambda: run_in_background(fn, *args, **kwargs))
