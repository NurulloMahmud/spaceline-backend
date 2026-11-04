import logging

import requests
from django.conf import settings

from rest_framework import status, views
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from users.permissions import IsInternalService

from .bids import relay_bid_status
from .permissions import IsHubService
from .services.driver_hub import BID_STATUSES
from .sync import remove_driver, upsert_driver

logger = logging.getLogger(__name__)


class HubDriverView(views.APIView):
    """POST /api/hub/drivers/: the hub pushes a driver's profile after accept and on every change."""
    permission_classes = [IsHubService]

    def post(self, request):
        payload = request.data if isinstance(request.data, dict) else {}
        try:
            driver = upsert_driver(payload)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception('hub driver sync failed')
            return Response({'error': f'could not store the driver: {exc}'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({'driver_id': driver.id})


class HubDriverRemoveView(views.APIView):
    """POST /api/hub/drivers/remove/: the driver left this carrier."""
    permission_classes = [IsHubService]

    def post(self, request):
        hub_driver_id = (request.data or {}).get('hub_driver_id')
        if not hub_driver_id:
            return Response({'error': 'hub_driver_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        driver = remove_driver(hub_driver_id)
        return Response({'driver_id': driver.id if driver else None, 'removed': driver is not None})


class HubBidStatusView(views.APIView):
    """
    POST /api/hub/bid-status/: what a dispatcher did on a load that has a driver-app bid.

    Called by the email agent when a bid is placed, booked or closed, and by the
    web app when a dispatcher declines an app bid. The hub target ids come out
    of the driver bids' notes unless the caller already has them.
    """
    permission_classes = [IsAuthenticated | IsInternalService]

    def post(self, request):
        data = request.data or {}
        load_id = str(data.get('load_id') or '').strip()
        bid_status = str(data.get('status') or '').strip()
        if not load_id:
            return Response({'error': 'load_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        if bid_status not in BID_STATUSES:
            return Response({'error': f'status must be one of {", ".join(BID_STATUSES)}'}, status=status.HTTP_400_BAD_REQUEST)

        placed_by = data.get('placed_by')
        user = getattr(request, 'user', None)
        if not placed_by and data.get('placed_by_user_id'):
            # The email agent knows the dispatcher only by id.
            from users.models import CustomUser
            who = CustomUser.objects.filter(id=data.get('placed_by_user_id')).first()
            if who:
                placed_by = f'{who.first_name} {who.last_name}'.strip() or who.username
        if not placed_by and user is not None and getattr(user, 'is_authenticated', False):
            placed_by = f'{user.first_name} {user.last_name}'.strip() or user.username

        target_ids = data.get('hub_target_ids') or []
        try:
            target_ids = [int(t) for t in target_ids]
        except (TypeError, ValueError):
            return Response({'error': 'hub_target_ids must be integers'}, status=status.HTTP_400_BAD_REQUEST)

        reached = relay_bid_status(
            load_id, bid_status,
            carrier_amount=data.get('carrier_amount'),
            driver_amount=data.get('driver_amount'),
            placed_by=placed_by,
            tenant_bid_id=data.get('tenant_bid_id'),
            note=data.get('note'),
            driver_id=data.get('driver_id'),
            target_ids=target_ids,
        )
        return Response({'targets': reached})


class CustomAlertsView(views.APIView):
    """
    The Custom Alerts experiment (sylectus/app/smtp_source.py): Sylectus
    alerts received by our own mail server, with how far ahead of the Gmail
    path each one was. Proxied from the sylectus API, which only listens on
    this machine. Who may look is settings.CUSTOM_ALERTS_EMAILS.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Accounts sign in with their email as the username, and the email
        # field itself is often empty, so either one may carry it.
        identities = {
            (getattr(request.user, field, '') or '').strip().lower()
            for field in ('email', 'username')
        }
        if not identities & set(settings.CUSTOM_ALERTS_EMAILS):
            return Response({'error': 'Custom Alerts is not available for this account.'}, status=status.HTTP_403_FORBIDDEN)
        params = {k: v for k, v in request.query_params.items() if k in ('limit', 'hours')}
        try:
            resp = requests.get(f"{settings.SYLECTUS_API_URL.rstrip('/')}/custom-alerts", params=params, timeout=15)
        except requests.RequestException as exc:
            logger.error('custom alerts: sylectus API unreachable: %s', exc)
            return Response({'error': 'The custom alerts service is not reachable right now.'}, status=status.HTTP_502_BAD_GATEWAY)
        try:
            body = resp.json()
        except ValueError:
            body = {'error': resp.text[:300]}
        return Response(body, status=resp.status_code)
