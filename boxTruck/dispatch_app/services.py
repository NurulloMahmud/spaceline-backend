"""Devices, session tokens and hub notifications for BTM Dispatch (SPEC-1.0 section 6)."""

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.utils import datetime_from_epoch

from hub.services import driver_hub

from . import state
from .background import after_commit
from .models import DispatchDevice

logger = logging.getLogger(__name__)

DSID_CLAIM = "dsid"
TOKEN_PREFIX = "dsp_"
SESSION_LIFETIME = timedelta(minutes=10)
NOTIFY_KINDS = ("broker_reply", "bid_won", "bid_booked")


class UserNotFound(Exception):
    pass


class UserInactive(Exception):
    pass


def new_device_token() -> str:
    # 30 random bytes are exactly 40 URL-safe characters.
    return TOKEN_PREFIX + secrets.token_urlsafe(30)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def live_device_for(token: str) -> DispatchDevice | None:
    if not token or not isinstance(token, str):
        return None
    device = (
        DispatchDevice.objects.select_related("user")
        .filter(token_hash=hash_token(token))
        .first()
    )
    return device if device is not None and device.is_live else None


def user_dict(user: Any) -> dict[str, Any]:
    return {
        "id": user.id,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": user.is_active,
    }


def _drop_keys(ids: list[Any]) -> None:
    if ids:
        transaction.on_commit(lambda: state.delete(*ids))


def revoke(queryset: Any, reason: str) -> list[Any]:
    """Revoke the live devices in queryset. Returns their ids; their Redis keys go after commit."""
    ids = list(queryset.filter(revoked_at__isnull=True).values_list("id", flat=True))
    if ids:
        DispatchDevice.objects.filter(id__in=ids, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason=reason
        )
        _drop_keys(ids)
    return ids


def revoke_user(user_id: int, reason: str) -> list[Any]:
    return revoke(DispatchDevice.objects.filter(user_id=user_id), reason)


def create_device(
    btm_user_id: int, hub_workspace_id: Any, platform: Any, device_label: Any
) -> tuple[DispatchDevice, str]:
    """A new live device for the user; their other live devices are revoked (replaced) first."""
    User = get_user_model()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=btm_user_id).first()
        if user is None:
            raise UserNotFound
        if not user.is_active:
            raise UserInactive
        revoke(
            DispatchDevice.objects.filter(user=user),
            DispatchDevice.REPLACED,
        )
        token = new_device_token()
        device = DispatchDevice.objects.create(
            user=user,
            token_hash=hash_token(token),
            hub_workspace_id=str(hub_workspace_id or "")[:64],
            platform=str(platform or "").strip().lower()[:16],
            device_label=str(device_label or "").strip()[:120],
        )
        dsid = device.id
        transaction.on_commit(lambda: state.set_state(dsid, True))
    return device, token


def mint_access(user: Any, dsid: Any) -> AccessToken:
    """A normal access token with the web login's claims, plus dsid, for 10 minutes."""
    # The web login's own serializer, so the claims never drift apart.
    from users.serializers import CustomTokenObtainPairSerializer

    access = CustomTokenObtainPairSerializer.get_token(user).access_token
    access.set_exp(lifetime=SESSION_LIFETIME)
    access[DSID_CLAIM] = str(dsid)
    return access


def exchange_session(device: DispatchDevice) -> dict[str, Any]:
    user = device.user
    access = mint_access(user, device.id)
    now = timezone.now()
    DispatchDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
    state.set_state(device.id, user.is_active)
    return {
        "access": str(access),
        # RFC 3339 in UTC with "Z", like every time the hub sends the app
        # (the hub passes this one on as it is).
        "expires_at": datetime_from_epoch(access["exp"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": user_dict(user),
    }


def has_live_device(user_id: int, active_only: bool = False) -> bool:
    devices = DispatchDevice.objects.filter(user_id=user_id, revoked_at__isnull=True)
    if active_only:
        devices = devices.filter(user__is_active=True)
    return devices.exists()


# --- Calls to the hub, fire and forget ---------------------------------------


def _send_notify(payload: dict[str, Any]) -> None:
    try:
        driver_hub.dispatch_notify(**payload)
    except driver_hub.DriverHubError as exc:
        logger.warning("dispatch notify %s to user %s not sent: %s", payload.get("kind"), payload.get("btm_user_id"), exc)


def notify(
    btm_user_id: int, kind: str, title: str, body: str, data: dict[str, Any] | None = None
) -> bool:
    """Tell the user's phone through the hub. After commit, in the background, never raises.

    False when skipped: hub not configured, unknown kind, or nobody linked (or
    the user is inactive: an inactive workspace gets no new notifications).
    """
    try:
        if kind not in NOTIFY_KINDS or not driver_hub.configured():
            return False
        if not has_live_device(int(btm_user_id), active_only=True):
            return False
        payload = {
            "btm_user_id": int(btm_user_id),
            "kind": kind,
            "title": str(title or "")[:200],
            "body": str(body or "")[:500],
            "data": {k: (None if v is None else str(v)) for k, v in (data or {}).items()},
        }
        after_commit(_send_notify, payload)
        return True
    except Exception:  # noqa: BLE001 - a notification never breaks the caller
        logger.exception("dispatch notify %s for user %s failed", kind, btm_user_id)
        return False


def _send_status(btm_user_id: int, active: bool) -> None:
    try:
        driver_hub.dispatch_user_status(btm_user_id, active)
    except driver_hub.DriverHubError as exc:
        logger.warning("dispatch user %s status %s not sent to the hub: %s", btm_user_id, active, exc)


def _send_unlink(btm_user_id: int) -> None:
    try:
        driver_hub.dispatch_unlink(btm_user_id)
    except driver_hub.DriverHubError as exc:
        logger.warning("dispatch unlink of user %s not sent to the hub: %s", btm_user_id, exc)


def user_status_changed(user_id: int, active: bool) -> None:
    """is_active flipped: flip the Redis key, tell the hub. Skipped when nobody is linked."""
    ids = list(
        DispatchDevice.objects.filter(user_id=user_id, revoked_at__isnull=True).values_list("id", flat=True)
    )
    if not ids:
        return

    def flip() -> None:
        for dsid in ids:
            state.set_state(dsid, active)

    transaction.on_commit(flip)
    if driver_hub.configured():
        after_commit(_send_status, user_id, active)


def unlink_on_hub(user_id: int) -> None:
    """The hub drops the user's workspace: after commit, in the background."""
    if driver_hub.configured():
        after_commit(_send_unlink, user_id)


def user_deleted(user_id: int) -> None:
    if revoke_user(user_id, DispatchDevice.USER_DELETED) and driver_hub.configured():
        after_commit(_send_unlink, user_id)
