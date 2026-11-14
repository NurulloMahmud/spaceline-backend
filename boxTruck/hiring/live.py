"""Live hiring board: tells open boards that a driver changed.

The email agent already keeps one event stream open per browser (the notification
bell uses it), so instead of a connection of its own the board listens there.
`notify(driver_id)` queues the driver; a background thread waits a moment so a burst
of saves (an import, a form with several files) goes out as one event, then posts
`hiring_changed {driver_ids}` to the agent's internal route. A board that gets it
reloads; nothing about the driver itself travels on the stream.

Failures never reach the request that changed the driver: the board still refreshes
every minute, and reloads whenever its stream reconnects.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

EVENT_TYPE = "hiring_changed"
COALESCE_SECONDS = 0.25
TIMEOUT_SECONDS = 3
WARN_EVERY_SECONDS = 300

_lock = threading.Lock()
_wake = threading.Event()
_pending: set[int] = set()
_worker: threading.Thread | None = None
_company_id: int | None = None
_last_warning = 0.0


def enabled() -> bool:
    # The test runner has no email agent to talk to.
    return bool(getattr(settings, "EMAIL_AGENT_INTERNAL_URL", "")) and "test" not in sys.argv[1:2]


def notify(driver_id: int | None) -> None:
    """Queues a driver whose board card changed. Call after the change is committed."""
    if not driver_id or not enabled():
        return
    global _worker
    with _lock:
        _pending.add(int(driver_id))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="hiring-live", daemon=True)
            _worker.start()
    _wake.set()


def _company() -> int | None:
    global _company_id
    if _company_id is None:
        from users.models import Company

        company = Company.objects.order_by("id").first()
        _company_id = company.id if company else None
    return _company_id


def _run() -> None:
    while True:
        _wake.wait()
        time.sleep(COALESCE_SECONDS)
        with _lock:
            _wake.clear()
            ids = sorted(_pending)
            _pending.clear()
        if ids:
            _send(ids)


def _send(driver_ids: list[int]) -> None:
    global _last_warning
    try:
        company_id = _company()
        if company_id is None:
            return
        res = requests.post(
            f"{settings.EMAIL_AGENT_INTERNAL_URL.rstrip('/')}/internal/v1/events",
            json={"type": EVENT_TYPE, "company_id": company_id, "payload": {"driver_ids": driver_ids}},
            headers={"X-Internal-Secret": settings.INTERNAL_SERVICE_SECRET},
            timeout=TIMEOUT_SECONDS,
        )
        res.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - the board falls back to polling
        now = time.monotonic()
        if now - _last_warning > WARN_EVERY_SECONDS:
            _last_warning = now
            logger.warning("Hiring board live event not sent (boards fall back to refreshing): %s", exc)
    finally:
        from django.db import connection

        # This thread has its own database connection (the company lookup); close it.
        connection.close()
