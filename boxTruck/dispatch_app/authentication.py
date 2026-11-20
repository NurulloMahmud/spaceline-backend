"""JWTAuthentication that also knows BTM Dispatch phone tokens.

A token without a `dsid` claim goes through simplejwt's JWTAuthentication
unchanged (the web login, every existing client). A token with `dsid` came
from the session exchange of one phone:

  - the device must be live and belong to the token's user, else
    401 {"detail": ..., "code": "device_revoked"};
  - an inactive user is let in (simplejwt alone would refuse them) but only
    on the Performance endpoints for their own data, else
    403 {"detail": ..., "code": "user_inactive"};
  - the device's last_seen_at moves at most once a minute.
"""

import re
import uuid
from datetime import timedelta
from typing import Any

from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.utils import get_md5_hash_password

DSID_CLAIM = "dsid"
LAST_SEEN_EVERY = timedelta(minutes=1)

# What an inactive user's phone may still open: the Performance tab, as the
# web Performance calendar reads it (the month's board and their own loads).
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
LEADERBOARD = re.compile(r"^/api/salaries/leaderboard/$")
LEADERBOARD_LOADS = re.compile(r"^/api/salaries/leaderboard/(\d+)/loads/$")


def device_revoked() -> AuthenticationFailed:
    return AuthenticationFailed(
        {"detail": "This phone is no longer linked.", "code": "device_revoked"},
        code="device_revoked",
    )


def inactive_allowed(request: Any, user: Any) -> bool:
    if request.method not in SAFE_METHODS:
        return False
    path = request.path_info
    if LEADERBOARD.match(path):
        return True
    own = LEADERBOARD_LOADS.match(path)
    return bool(own) and int(own.group(1)) == user.pk


class DispatchJWTAuthentication(JWTAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is None:
            return None
        user, token = result
        if token.get(DSID_CLAIM) is not None and not user.is_active:
            if not inactive_allowed(request, user):
                raise PermissionDenied(
                    {"detail": "Your account is turned off.", "code": "user_inactive"}
                )
        return result

    def get_user(self, validated_token):
        dsid = validated_token.get(DSID_CLAIM)
        if dsid is None:
            return super().get_user(validated_token)
        return self._device_user(validated_token, dsid)

    def _device_user(self, validated_token, dsid):
        from .models import DispatchDevice

        try:
            user_id = validated_token[api_settings.USER_ID_CLAIM]
        except KeyError:
            raise InvalidToken("Token contained no recognizable user identification")
        try:
            device_id = uuid.UUID(str(dsid))
        except (TypeError, ValueError):
            raise device_revoked()

        device = (
            DispatchDevice.objects.select_related("user").filter(pk=device_id).first()
        )
        if (
            device is None
            or not device.is_live
            or str(getattr(device.user, api_settings.USER_ID_FIELD)) != str(user_id)
        ):
            raise device_revoked()
        user = device.user

        if api_settings.CHECK_REVOKE_TOKEN:
            if validated_token.get(api_settings.REVOKE_TOKEN_CLAIM) != get_md5_hash_password(user.password):
                raise AuthenticationFailed(
                    "The user's password has been changed.", code="password_changed"
                )

        now = timezone.now()
        if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_EVERY:
            DispatchDevice.objects.filter(pk=device.pk).update(last_seen_at=now)
        return user
