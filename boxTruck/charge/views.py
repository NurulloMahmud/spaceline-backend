"""Frontend-facing billing endpoints.

Every view: authenticate the JWT user -> resolve the paying company -> call the billing
service with our API key -> return its JSON. The frontend never calls billing directly and
never sees ``BILLING_API_KEY``.

Payer identity (``external_user_id`` in billing) = the company id. All users of a company
share one payment method and one transaction history.
"""
import logging

from django.conf import settings
from rest_framework import status, views
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .client import BillingError, client
from .permissions import CanManageBilling
from .serializers import CardSetupRequestSerializer, TransactionQuerySerializer

logger = logging.getLogger(__name__)


def _company(request):
    company = getattr(request.user, "company", None)
    if company is None:
        raise ValidationError("Your account is not linked to a company.")
    return company


def _external_id(request):
    return str(_company(request).id)


def _is_unknown_payer(exc):
    return exc.status_code == 404 and "unknown external_user_id" in str(exc.detail)


def _billing(fn, *, empty_on_new_payer=None):
    """Call the billing client and turn BillingError into a matching DRF Response.

    A company that has never touched billing has no payer record yet — for read views we return
    ``empty_on_new_payer`` (an empty list / a blank summary) so the frontend gets one stable
    shape instead of a 404.
    """
    try:
        return Response(fn())
    except BillingError as exc:
        if empty_on_new_payer is not None and _is_unknown_payer(exc):
            return Response(empty_on_new_payer)
        code = exc.status_code if 400 <= exc.status_code < 600 else status.HTTP_502_BAD_GATEWAY
        detail = exc.detail if isinstance(exc.detail, dict) else {"detail": exc.detail}
        return Response(detail, status=code)


class SummaryView(views.APIView):
    """GET /api/charge/summary/ — dashboard header: default card, card count, active plans,
    next charge, lifetime paid."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        eid = _external_id(request)
        return _billing(
            lambda: client.summary(eid),
            empty_on_new_payer={
                "external_user_id": eid,
                "has_card": False,
                "cards_count": 0,
                "default_card": None,
                "active_recurring_count": 0,
                "next_charge": None,
                "lifetime_paid": [],
            },
        )


class UpcomingView(views.APIView):
    """GET /api/charge/upcoming/ — charges the company will be billed (next date + amount)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        eid = _external_id(request)
        return _billing(lambda: client.upcoming(eid), empty_on_new_payer=[])


class TransactionsView(views.APIView):
    """GET /api/charge/transactions/?type=&period=YYYY-MM&limit=&offset= — the paid ledger."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        q = TransactionQuerySerializer(data=request.query_params)
        q.is_valid(raise_exception=True)
        eid = _external_id(request)
        return _billing(
            lambda: client.transactions(eid, **q.validated_data), empty_on_new_payer=[]
        )


class RecurringView(views.APIView):
    """GET /api/charge/recurring/ — the company's recurring plans (read-only here; plans are
    managed by the billing owner, not the tenant)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        eid = _external_id(request)
        status_ = request.query_params.get("status")
        return _billing(
            lambda: client.recurring(eid, status=status_), empty_on_new_payer=[]
        )


class CardsView(views.APIView):
    """GET  /api/charge/cards/         — list active cards
    POST /api/charge/cards/setup/    (see CardSetupView) — add a card"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        eid = _external_id(request)
        return _billing(lambda: client.list_cards(eid), empty_on_new_payer=[])


class CardSetupView(views.APIView):
    """POST /api/charge/cards/setup/ — start a Stripe-hosted card capture.

    Returns ``checkout_url``; the frontend redirects the browser there. Stripe returns the user
    to ``return_url`` with ``?billing_setup_session=<id>&checkout_session_id=cs_...`` — the card
    is only confirmed saved once GET /api/charge/cards/setup/<id>/ reports ``completed``.
    """

    permission_classes = [CanManageBilling]

    def post(self, request):
        body = CardSetupRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        company = _company(request)
        return_url = body.validated_data.get("return_url") or settings.BILLING_CARD_RETURN_URL
        return _billing(
            lambda: client.create_card_setup_session(
                str(company.id),
                return_url=return_url,
                email=company.email or None,
                name=company.name or None,
                client_reference=body.validated_data.get("client_reference"),
            )
        )


class CardSetupStatusView(views.APIView):
    """GET /api/charge/cards/setup/<session_id>/ — poll after the Stripe redirect."""

    permission_classes = [IsAuthenticated]

    def get(self, request, session_id):
        _external_id(request)  # ensure the caller has a company
        return _billing(lambda: client.get_card_setup_session(session_id))


class CardDefaultView(views.APIView):
    """POST /api/charge/cards/<card_id>/default/ — make this card the one recurring charges use."""

    permission_classes = [CanManageBilling]

    def post(self, request, card_id):
        eid = _external_id(request)
        return _billing(lambda: client.set_default_card(eid, card_id))


class CardDeleteView(views.APIView):
    """DELETE /api/charge/cards/<card_id>/ — detach a card."""

    permission_classes = [CanManageBilling]

    def delete(self, request, card_id):
        eid = _external_id(request)
        return _billing(lambda: client.delete_card(eid, card_id))
