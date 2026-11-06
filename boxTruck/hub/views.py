import logging

from django.db.models import Q
from rest_framework import status, views
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from users.permissions import IsInternalService

from .bids import relay_bid_status, resolve_app_bids
from .decisions import decision_dict, record_declines
from .models import DriverBidDecision
from .permissions import IsHubService
from .services.driver_hub import BID_STATUSES
from .sync import INVITE_STATUSES, remove_driver, set_invite_status, upsert_driver

logger = logging.getLogger(__name__)


class HubDriverView(views.APIView):
    """POST /api/hub/drivers/: the hub pushes a driver's profile after accept and on every change."""

    permission_classes = [IsHubService]

    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            driver = upsert_driver(payload)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("hub driver sync failed")
            return Response(
                {"error": f"could not store the driver: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response({"driver_id": driver.id})


class HubDriverRemoveView(views.APIView):
    """POST /api/hub/drivers/remove/: the driver left this carrier."""

    permission_classes = [IsHubService]

    def post(self, request):
        hub_driver_id = (request.data or {}).get("hub_driver_id")
        if not hub_driver_id:
            return Response(
                {"error": "hub_driver_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        driver = remove_driver(hub_driver_id)
        return Response(
            {"driver_id": driver.id if driver else None, "removed": driver is not None}
        )


class HubInviteStatusView(views.APIView):
    """
    POST /api/hub/invites/status/: the driver answered an invite in the app.

    Body {invite_id, email, status: declined | accepted}. An invite no driver
    here has is answered 200 {"ok": false}, so the hub does not retry it.
    """

    permission_classes = [IsHubService]

    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        invite_status = str(data.get("status") or "").strip().lower()
        if invite_status not in INVITE_STATUSES:
            return Response(
                {"error": f'status must be one of {", ".join(INVITE_STATUSES)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not data.get("invite_id") and not data.get("email"):
            return Response(
                {"error": "invite_id or email is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        driver = set_invite_status(
            data.get("invite_id"), data.get("email"), invite_status
        )
        if driver is None:
            logger.info(
                "hub invite %s (%s) %s: no driver has it",
                data.get("invite_id"),
                data.get("email"),
                invite_status,
            )
            return Response({"ok": False})
        return Response(
            {"ok": True, "driver_id": driver.id, "hub_status": driver.hub_status}
        )


class HubBidStatusView(views.APIView):
    """
    POST /api/hub/bid-status/: what a dispatcher did on a load that has a driver-app bid.

    Called by the email agent when a bid is placed, booked or closed, and by the
    web app when a dispatcher declines an app bid. The driver-app bids are read
    from the load (filtered by `driver_id` if given) unless the caller names
    them: `driver_bid_ids` (their loads-service ids) or, for old bids,
    `hub_target_ids`. A decline is also kept here (DriverBidDecision), before
    and whatever the hub answers; the response lists those bids.
    """

    permission_classes = [IsAuthenticated | IsInternalService]

    def post(self, request):
        data = request.data or {}
        load_id = str(data.get("load_id") or "").strip()
        bid_status = str(data.get("status") or "").strip()
        if not load_id:
            return Response(
                {"error": "load_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )
        if bid_status not in BID_STATUSES:
            return Response(
                {"error": f'status must be one of {", ".join(BID_STATUSES)}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        placed_by = data.get("placed_by")
        user = getattr(request, "user", None)
        who = None
        if data.get("placed_by_user_id"):
            # The email agent knows the dispatcher only by id.
            from users.models import CustomUser

            who = CustomUser.objects.filter(id=data.get("placed_by_user_id")).first()
        if not placed_by and who:
            placed_by = f"{who.first_name} {who.last_name}".strip() or who.username
        if (
            not placed_by
            and user is not None
            and getattr(user, "is_authenticated", False)
        ):
            placed_by = f"{user.first_name} {user.last_name}".strip() or user.username

        target_ids = data.get("hub_target_ids") or []
        try:
            target_ids = [int(t) for t in target_ids]
        except (TypeError, ValueError):
            return Response(
                {"error": "hub_target_ids must be integers"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        driver_bid_ids = data.get("driver_bid_ids") or []
        if not isinstance(driver_bid_ids, list):
            return Response(
                {"error": "driver_bid_ids must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        driver_bid_ids = [str(b) for b in driver_bid_ids]
        driver_id = data.get("driver_id")
        if driver_id not in (None, ""):
            try:
                driver_id = int(driver_id)
            except (TypeError, ValueError):
                return Response(
                    {"error": "driver_id must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            driver_id = None

        app_bids = None
        declined: list[DriverBidDecision] = []
        if bid_status == "declined":
            # Ours first, whatever the hub then says: the decision is made here.
            app_bids = resolve_app_bids(load_id, driver_id, target_ids, driver_bid_ids)
            decider = (
                user
                if user is not None and getattr(user, "is_authenticated", False)
                else who
            )
            declined = record_declines(
                load_id,
                app_bids,
                note=data.get("note"),
                user=decider,
                driver_id=driver_id,
            )

        reached = relay_bid_status(
            load_id,
            bid_status,
            carrier_amount=data.get("carrier_amount"),
            driver_amount=data.get("driver_amount"),
            placed_by=placed_by,
            tenant_bid_id=data.get("tenant_bid_id"),
            note=data.get("note"),
            driver_id=driver_id,
            target_ids=target_ids,
            driver_bid_ids=driver_bid_ids,
            app_bids=app_bids,
        )
        return Response(
            {"targets": reached, "declined_bid_ids": [d.bid_id for d in declined]}
        )


MAX_DECISION_IDS = 500


def _id_list(value: str | None) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in (value or "").split(",") if v.strip()))


class HubDriverBidDecisionsView(views.APIView):
    """
    GET /api/hub/driver-bid-decisions/?load_ids=a,b or ?bid_ids=x,y (both: either matches).

    The declines kept by bid-status, for the Load board's Bid history: one row
    per declined driver bid, newest first. At most 500 ids per parameter.
    """

    permission_classes = [IsAuthenticated | IsInternalService]

    def get(self, request: Request) -> Response:
        load_ids = _id_list(request.query_params.get("load_ids"))
        bid_ids = _id_list(request.query_params.get("bid_ids"))
        if not load_ids and not bid_ids:
            return Response(
                {"error": "load_ids or bid_ids is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(load_ids) > MAX_DECISION_IDS or len(bid_ids) > MAX_DECISION_IDS:
            return Response(
                {"error": f"at most {MAX_DECISION_IDS} ids per parameter"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        match = Q()
        if load_ids:
            match |= Q(load_id__in=load_ids)
        if bid_ids:
            match |= Q(bid_id__in=bid_ids)
        rows = DriverBidDecision.objects.filter(match).select_related("decided_by")
        return Response([decision_dict(row) for row in rows])
