"""The Redis key the other carrier services check for a phone token.

`dispatch:dsid:<dsid>` is `active` or `inactive` while the device is live and
in use: set when the device is made, refreshed (30 minute TTL) on every
session exchange, flipped when the user's is_active changes, deleted on
revoke. The Go loads service and the email agent read it for any token with a
`dsid` claim; a missing key means the device is revoked (or idle past the TTL,
when its last access token has long expired anyway).

Django itself checks the database, so Redis being down never blocks Django;
a failed write is logged and the next session exchange writes again.
"""

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "dispatch:dsid:"
TTL_SECONDS = 30 * 60
ACTIVE = "active"
INACTIVE = "inactive"
SOCKET_TIMEOUT = 2

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


def get_state(dsid: Any) -> str | None:
    """For tests and the README's checks: the stored value, or None."""
    return get_client().get(key(dsid))
