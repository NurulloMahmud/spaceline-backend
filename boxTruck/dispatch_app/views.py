"""BTM Dispatch endpoints (SPEC-1.0 sections 4 to 6).

Web (normal JWT): link-code/, device/.
Hub (X-Hub-Token): devices/, session/, devices/revoke/.
Email agent (X-Internal-Secret): internal/notify/.
Errors are {"error": ..., "code": ...} like the hub's own.
"""

import logging
from typing import Any

from rest_framework import status, views
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from hub.permissions import IsHubService
from hub.services import driver_hub
from users.permissions import IsInternalService

from . import services
from .mailbox import mailbox_for
from .models import DispatchDevice

logger = logging.getLogger(__name__)


def error(http_status: int, code: str, message: str) -> Response:
    return Response({"error": message, "code": code}, status=http_status)


def _body(request: Request) -> dict[str, Any]:
    return request.data if isinstance(request.data, dict) else {}


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# --- Web --------------------------------------------------------------------


class LinkCodeView(views.APIView):
    """POST /api/dispatch-app/link-code/: a 2 minute code for the signed-in user to scan in the app."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        if not driver_hub.configured():
            return error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "hub_not_configured",
                "BTM Dispatch is not set up for this company.",
            )
        user = request.user
        try:
            reply = driver_hub.dispatch_link_code(
                btm_user_id=user.id,
                first_name=user.first_name,
                last_name=user.last_name,
                email=user.email,
                active=user.is_active,
            )
        except driver_hub.DriverHubError as exc:
            if exc.status_code == 409 and exc.code == "user_inactive":
                return error(status.HTTP_409_CONFLICT, "user_inactive", "Your account is turned off.")
            logger.warning("dispatch link code for user %s failed: %s", user.id, exc)
            return error(
                status.HTTP_502_BAD_GATEWAY,
                "hub_unreachable",
                "Could not reach BTM Dispatch. Try again.",
            )
        reply = reply if isinstance(reply, dict) else {}
        return Response(
            {
                "code": reply.get("code"),
                "qr_text": reply.get("qr_text"),
                "expires_at": reply.get("expires_at"),
            },
            status=status.HTTP_201_CREATED,
        )


class DeviceView(views.APIView):
    """GET /api/dispatch-app/device/: the linked phone, or null. DELETE: unlink it."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        device = DispatchDevice.objects.filter(
            user=request.user, revoked_at__isnull=True
        ).first()
        if device is None:
            return Response({"device": None})
        return Response(
            {
                "device": {
                    "platform": device.platform,
                    "device_label": device.device_label,
                    "linked_at": device.created_at,
                    "last_seen_at": device.last_seen_at,
                }
            }
        )

    def delete(self, request: Request) -> Response:
        user_id = request.user.id
        services.revoke_user(user_id, DispatchDevice.UNLINKED)
        # Also when nothing was live here: it clears a workspace the hub still shows.
        # In the background: the phone is cut off already, and its next session
        # call drops the workspace even if the hub misses this.
        services.unlink_on_hub(user_id)
        return Response({"ok": True})


# --- Hub ----------------------------------------------------------------------


class HubView(views.APIView):
    authentication_classes: list = []
    permission_classes = [IsHubService]


class DevicesView(HubView):
    """POST /api/dispatch-app/devices/: the hub links a phone; the user's other devices are revoked."""

    def post(self, request: Request) -> Response:
        data = _body(request)
        user_id = _int(data.get("btm_user_id"))
        if user_id is None:
            return error(status.HTTP_404_NOT_FOUND, "user_not_found", "No such user.")
        try:
            device, token = services.create_device(
                user_id,
                data.get("hub_workspace_id"),
                data.get("platform"),
                data.get("device_label"),
            )
        except services.UserNotFound:
            return error(status.HTTP_404_NOT_FOUND, "user_not_found", "No such user.")
        except services.UserInactive:
            return error(status.HTTP_409_CONFLICT, "user_inactive", "This user is turned off.")
        return Response(
            {
                "device_id": str(device.id),
                "device_token": token,
                "user": services.user_dict(device.user),
                "mailbox": mailbox_for(device.user_id),
            },
            status=status.HTTP_201_CREATED,
        )


class SessionView(HubView):
    """POST /api/dispatch-app/session/: device token -> 10 minute access token."""

    def post(self, request: Request) -> Response:
        device = services.live_device_for(_body(request).get("device_token"))
        if device is None:
            return error(
                status.HTTP_401_UNAUTHORIZED, "device_revoked", "This phone is no longer linked."
            )
        reply = services.exchange_session(device)
        reply["mailbox"] = mailbox_for(device.user_id)
        return Response(reply)


class RevokeView(HubView):
    """POST /api/dispatch-app/devices/revoke/: 200 also when already revoked or unknown."""

    def post(self, request: Request) -> Response:
        token = _body(request).get("device_token")
        revoked: list = []
        if token and isinstance(token, str):
            revoked = services.revoke(
                DispatchDevice.objects.filter(token_hash=services.hash_token(token)),
                DispatchDevice.HUB,
            )
        return Response({"revoked": bool(revoked)})


# --- Email agent -----------------------------------------------------------------


class InternalNotifyView(views.APIView):
    """POST /api/dispatch-app/internal/notify/: the email agent's broker_reply, bid_won, bid_booked.

    Body {btm_user_id, kind, title, body, data}. Answers at once; the hub is
    called after, in the background. {"queued": false} when skipped (nobody
    linked, user inactive, hub not configured).
    """

    authentication_classes: list = []
    permission_classes = [IsInternalService]

    def post(self, request: Request) -> Response:
        data = _body(request)
        user_id = _int(data.get("btm_user_id"))
        kind = str(data.get("kind") or "")
        if user_id is None or kind not in services.NOTIFY_KINDS:
            return error(
                status.HTTP_400_BAD_REQUEST,
                "bad_request",
                f"btm_user_id and kind ({', '.join(services.NOTIFY_KINDS)}) are required",
            )
        extra = data.get("data") if isinstance(data.get("data"), dict) else {}
        queued = services.notify(user_id, kind, data.get("title"), data.get("body"), extra)
        return Response({"queued": queued})
