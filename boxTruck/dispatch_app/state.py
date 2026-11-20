"""The Redis key the other carrier services check for a phone token.

`dispatch:dsid:<dsid>` is `active` or `inactive` while the device is live and
in use: set when the device is made, refreshed (11 minute TTL, one minute
past the 10 minute access token) on every session exchange, flipped when the user's is_active changes, deleted on
revoke. The Go loads service and the email agent read it for any token with a
`dsid` claim; a missing key means the device is revoked (or idle past the TTL,
when its last access token has long expired anyway).

Django itself checks the database, so Redis being down never blocks Django;
a failed write is logged and the next session exchange writes again.
"""

import logging
import time
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "dispatch:dsid:"
TTL_SECONDS = 11 * 60
ACTIVE = "active"
INACTIVE = "inactive"
SOCKET_TIMEOUT = 2
# A failed delete of a revoked device's key is tried again: after 0.5, 1, 2, 4
# and 8 seconds (about 15 s in all), in the background thread.
DELETE_RETRY_DELAYS = (0.5, 1, 2, 4, 8)
_sleep = time.sleep

_client: Any = None


def key(dsid: Any) -> str:
    return f"{KEY_PREFIX}{dsid}"


def get_client() -> Any:
    global _client
    if _client is None:
        import redis

        _client = redis.Redis.from_url(
            settings.DISPATCH_REDIS_URL,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_TIMEOUT,
            decode_responses=True,
        )
    return _client


def set_state(dsid: Any, active: bool) -> bool:
    """Write active or inactive with a fresh TTL. False when Redis failed."""
    try:
        get_client().set(key(dsid), ACTIVE if active else INACTIVE, ex=TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.error("dispatch redis set %s failed: %s", key(dsid), exc)
        return False


def delete(*dsids: Any) -> bool:
    """Drop the keys of revoked devices. False when Redis failed."""
    if not dsids:
        return True
    try:
        get_client().delete(*[key(d) for d in dsids])
        return True
    except Exception as exc:  # noqa: BLE001 - never break the caller
        logger.error("dispatch redis delete %s failed: %s", [key(d) for d in dsids], exc)
        return False


def delete_with_retry(*dsids: Any) -> bool:
    """delete(), tried again with backoff until it works. Blocks: call it off the request."""
    if delete(*dsids):
        return True
    for delay in DELETE_RETRY_DELAYS:
        _sleep(delay)
        if delete(*dsids):
            return True
    logger.error("dispatch redis keys %s still there after retries", [key(d) for d in dsids])
    return False


def drop(*dsids: Any) -> None:
    """Delete now; on failure keep trying in a background thread."""
    if dsids and not delete(*dsids):
        from .background import run_in_background

        run_in_background(delete_with_retry, *dsids)


def get_state(dsid: Any) -> str | None:
    """For tests and the README's checks: the stored value, or None."""
    return get_client().get(key(dsid))
