"""BTM Dispatch, the dispatcher phone app (SPEC-1.0 section 6).

Phone tokens: an access token with a `dsid` claim came from the phone's session
exchange in Django. It is good here only while Django's Redis key
`dispatch:dsid:<dsid>` says `active`: a missing key means the device was
revoked (401 device_revoked), `inactive` means the user is turned off, and an
inactive user's Inbox is locked, so nothing here is open to them (403
user_inactive). Redis failing rejects phone tokens (503) and never touches web
tokens, which carry no `dsid`.

Notifications: a broker reply on a conversation owned by the user
(broker_reply) and the user's own bid winning (bid_won: a rate confirmation
arrived) or being booked (bid_booked) go to Django's
/api/dispatch-app/internal/notify/, which passes them to the hub when the user
has a linked phone. Queued on the database session and sent after it
commits, in a worker thread with a short timeout; nothing here ever raises
into the caller.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx
from sqlalchemy import event
from sqlalchemy.orm import Session, object_session

from config.settings import config
from database import models

logger = logging.getLogger(__name__)

DSID_CLAIM = "dsid"
KEY_PREFIX = "dispatch:dsid:"
ACTIVE = "active"
INACTIVE = "inactive"
REDIS_TIMEOUT_SECONDS = 2
NOTIFY_TIMEOUT_SECONDS = 3
NOTIFY_PATH = "/api/dispatch-app/internal/notify/"
# How often an open event stream re-reads the key of a phone token.
STREAM_RECHECK_SECONDS = 15

# The web's "won" (deals.js statusOf): a rate confirmation came in, matching or not.
WON_STATUSES = (models.RATECON_RECEIVED, models.MISMATCH)
_PENDING = "dispatch_notices"

_redis: Any = None
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dispatch-notify")


# --- Phone tokens --------------------------------------------------------------


def redis_client() -> Any:
    global _redis
    if _redis is None:
        import redis

        _redis = redis.Redis.from_url(
            config.DISPATCH_REDIS_URL,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            decode_responses=True,
        )
    return _redis


def device_state(dsid: Any) -> Optional[str]:
    """`active`, `inactive`, or None when the key is gone. Raises when Redis fails."""
    value = redis_client().get(f"{KEY_PREFIX}{dsid}")
    if isinstance(value, bytes):
        value = value.decode()
    return value


# --- Notifications ---------------------------------------------------------------


def _route(negotiation: models.Negotiation) -> str:
    snapshot = negotiation.load_snapshot or {}
    origin = str(snapshot.get("pick_up_at") or "").strip()
    destination = str(snapshot.get("deliver_to") or "").strip()
    if origin and destination:
        return f"{origin} to {destination}"
    return origin or destination


def first_line(text: Optional[str], limit: int = 200) -> str:
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line:
            return line[:limit]
    return ""


def queue(session: Optional[Session], payload: dict[str, Any]) -> None:
    """Send after this session commits (dropped if it rolls back)."""
    if not payload.get("btm_user_id"):
        return
    if session is None:
        _submit(payload)
        return
    session.info.setdefault(_PENDING, []).append(payload)


def broker_replied(
    session: Session, negotiation: models.Negotiation, from_email: str, body_text: Optional[str]
) -> None:
    """A broker reply landed on the conversation; its owner hears about it."""
    try:
        queue(
            session,
            {
                "btm_user_id": negotiation.dispatcher_user_id,
                "kind": "broker_reply",
                "title": f"{negotiation.broker_name or from_email or 'Broker'} replied",
                "body": first_line(body_text) or "New message",
                "data": {"thread_id": str(negotiation.id), "load_id": negotiation.load_uuid},
            },
        )
    except Exception:  # noqa: BLE001 - a notification never breaks the inbound path
        logger.exception("dispatch broker_reply not queued for negotiation %s", negotiation.id)


def _bid_notice(negotiation: models.Negotiation, kind: str) -> dict[str, Any]:
    return {
        "btm_user_id": negotiation.dispatcher_user_id,
        "kind": kind,
        "title": "Your bid won" if kind == "bid_won" else "Load booked",
        "body": _route(negotiation),
        # The bid as the web's Bids tab knows it: the negotiation.
        "data": {"load_id": negotiation.load_uuid, "bid_id": str(negotiation.id)},
    }


@event.listens_for(models.Negotiation.status, "set", active_history=True)
def _status_set(target: models.Negotiation, value: Any, oldvalue: Any, initiator: Any) -> None:
    try:
        if value == oldvalue:
            return
        if value == models.BOOKED:
            kind = "bid_booked"
        elif value in WON_STATUSES and oldvalue not in (*WON_STATUSES, models.BOOKED):
            kind = "bid_won"
        else:
            return
        session = object_session(target)
        if session is None or target.id is None:
            return  # not a stored negotiation yet
        queue(session, _bid_notice(target, kind))
    except Exception:  # noqa: BLE001 - a notification never breaks a status change
        logger.exception("dispatch bid notice not queued")


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    for payload in session.info.pop(_PENDING, []):
        _submit(payload)


@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction: Any) -> None:
    # Every rollback() of the whole transaction, also one that never reached the
    # database; a savepoint's rollback keeps what the outer transaction queued.
    if not getattr(previous_transaction, "nested", False):
        session.info.pop(_PENDING, None)


def _submit(payload: dict[str, Any]) -> None:
    try:
        _pool.submit(_deliver, payload)
    except Exception:  # noqa: BLE001 - shutting down
        logger.warning("dispatch notice %s dropped", payload.get("kind"))


def _deliver(payload: dict[str, Any]) -> None:
    if not config.BOXTRUCK_BASE_URL:
        return
    try:
        resp = httpx.post(
            f"{config.BOXTRUCK_BASE_URL}{NOTIFY_PATH}",
            json=payload,
            headers={"X-Internal-Secret": config.BOXTRUCK_INTERNAL_SECRET},
            timeout=NOTIFY_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            logger.warning("dispatch notice %s -> %s", payload.get("kind"), resp.status_code)
    except Exception as e:  # noqa: BLE001 - fire and forget
        logger.warning("dispatch notice %s not sent: %s", payload.get("kind"), e)
