"""
Endpoints used by the email-agent service (server-to-server, no JWT).

They exist separately from the dispatcher-facing views because the regular
viewsets scope every query to `request.user`, which internal callers do not
have. Authentication is the shared `X-Internal-Secret` header.
"""
import logging

from django.db import transaction
from django.utils.dateparse import parse_datetime
from rest_framework import status, views
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from users.models import Company, CustomUser
from users.permissions import IsInternalService

from .models import Broker, Load, LoadFile, LoadHistory, LoadStatus, LoadStop

logger = logging.getLogger(__name__)


def _parse_stop_datetime(value):
    """Ratecon dates arrive as a string or a list of strings per stop."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not value:
        return None
    return parse_datetime(value.replace('/', '-')) or parse_datetime(value)


class InternalCompanyProfileView(views.APIView):
    """Letterhead fields the email-agent renders into the bid email template."""
    permission_classes = [IsInternalService]

    def get(self, request, company_id):
        company = Company.objects.filter(id=company_id).first()
        if not company:
            return Response({"error": "Company not found"}, status=status.HTTP_404_NOT_FOUND)

        logo_url = None
        if company.logo:
            try:
                logo_url = request.build_absolute_uri(company.logo.url)
            except ValueError:
                logo_url = None

        return Response({
            "id": company.id,
            "name": company.name,
            "mc": company.mc,
            "address": company.address,
            "email": company.email,
            "phone_number": company.phone_number,
            "website": company.website,
            "logo_url": logo_url,
            "bid_validity_minutes": company.bid_validity_minutes,
        })


class InternalDispatcherView(views.APIView):
    """Dispatcher identity for the bid email signature."""
    permission_classes = [IsInternalService]

    def get(self, request, user_id):
        user = CustomUser.objects.select_related('company').filter(id=user_id).first()
        if not user:
            return Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "full_name": f"{user.first_name} {user.last_name}".strip(),
            "username": user.username,
            "company_id": user.company_id,
        })


class InternalBrokerResolveView(views.APIView):
    """
    Find a broker by MC (preferred) or name, creating one when the load's
    broker is not in the TMS yet. Booking cannot proceed without a Broker row.
    """
    permission_classes = [IsInternalService]

    def post(self, request):
        mc = (request.data.get('mc') or '').strip()
        name = (request.data.get('name') or '').strip()
        email = (request.data.get('email') or '').strip()

        if not mc and not name:
            return Response(
                {"error": "mc or name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        broker = None
        if mc:
            broker = Broker.objects.filter(mc=mc).first()
        if not broker and name:
            broker = Broker.objects.filter(name__iexact=name).first()

        created = False
        if not broker:
            if not mc:
                # `mc` is unique and non-null on Broker; synthesise a placeholder
                # so dispatch can correct it later rather than blocking the booking.
                mc = f"UNKNOWN-{name[:40]}"
                if Broker.objects.filter(mc=mc).exists():
                    broker = Broker.objects.filter(mc=mc).first()
            if not broker:
                broker = Broker.objects.create(
                    name=name or f"Broker {mc}",
                    mc=mc,
                    email=email or None,
                )
                created = True
        elif email and not broker.email:
            broker.email = email
            broker.save(update_fields=['email'])

        return Response({
            "id": broker.id,
            "name": broker.name,
            "mc": broker.mc,
            "email": broker.email,
            "ai_type": broker.ai_type,
            "created": created,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class InternalRateConParseView(views.APIView):
    """
    Same parsing the dispatcher-facing rate-con-upload view performs, reachable
    without a JWT. Broker is optional: without one we default to the Gemini
    parser, which is what `Broker.ai_type` defaults to in practice.
    """
    permission_classes = [IsInternalService]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        from .gemini import parse_rate_confirmation_gemini
        from .utils import parse_rate_confirmation

        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({"error": "No file provided"}, status=status.HTTP_400_BAD_REQUEST)
        if not uploaded_file.name.lower().endswith('.pdf'):
            return Response({"error": "Only PDF files are accepted"}, status=status.HTTP_400_BAD_REQUEST)

        broker_id = request.data.get('broker')
        broker = Broker.objects.filter(id=broker_id).first() if broker_id else None
        ai_type = broker.ai_type if broker else 'Gemini'

        try:
            if ai_type == 'Gemini':
                parsed_data = parse_rate_confirmation_gemini(uploaded_file)
            else:
                parsed_data = parse_rate_confirmation(uploaded_file)
        except Exception as e:
            logger.error("internal ratecon parse failed | file=%s | error=%s", uploaded_file.name, e)
            return Response(
                {"error": f"Failed to process file: {e}"},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        return Response({"parsed_data": parsed_data}, status=status.HTTP_200_OK)


class InternalBookLoadView(views.APIView):
    """
    Create a load, its stops and its rate confirmation file in one transaction.

    The email-agent calls this only after it has verified the ratecon against
    the negotiated terms, so there is no approval step here. Everything is
    written or nothing is.
    """
    permission_classes = [IsInternalService]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        import json

        payload = request.data.get('payload')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return Response({"error": "payload is not valid JSON"}, status=status.HTTP_400_BAD_REQUEST)
        if not payload:
            payload = request.data

        company_id = payload.get('company_id')
        driver_id = payload.get('driver_id')
        broker_id = payload.get('broker_id')

        if not company_id:
            return Response({"error": "company_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        company = Company.objects.filter(id=company_id).first()
        if not company:
            return Response({"error": "Company not found"}, status=status.HTTP_404_NOT_FOUND)

        load_number = (payload.get('load_number') or '').strip() or None
        if load_number:
            existing = Load.objects.filter(company=company, load_number=load_number).first()
            if existing:
                return Response({
                    "load_id": existing.id,
                    "shipment": existing.shipment,
                    "load_number": existing.load_number,
                    "already_existed": True,
                }, status=status.HTTP_200_OK)

        booked_by_id = payload.get('booked_by_id')
        booked_by = CustomUser.objects.filter(id=booked_by_id).first() if booked_by_id else None

        status_name = payload.get('status_name') or 'Booked'
        load_status = LoadStatus.objects.filter(name__iexact=status_name).first()

        try:
            with transaction.atomic():
                load = Load.objects.create(
                    company=company,
                    driver_id=driver_id,
                    broker_id=broker_id,
                    booked_by=booked_by,
                    created_by=booked_by,
                    status=load_status,
                    load_number=load_number,
                    carrier_pay=payload.get('carrier_pay'),
                    driver_pay=payload.get('driver_pay'),
                    pickup_date=_parse_stop_datetime(payload.get('pickup_date')),
                    drop_date=_parse_stop_datetime(payload.get('drop_date')),
                    note=payload.get('note'),
                    dispatcher_note=payload.get('dispatcher_note'),
                )

                stops = payload.get('stops') or []
                for order, stop in enumerate(stops, start=1):
                    LoadStop.objects.create(
                        load=load,
                        address=stop.get('address'),
                        city=stop.get('city'),
                        state=(stop.get('state') or '')[:2] or None,
                        zipcode=stop.get('zip_code') or stop.get('zipcode'),
                        order=order,
                        load_pickup=bool(stop.get('load_pickup')),
                        load_drop=bool(stop.get('load_drop')),
                        last_location=order == len(stops),
                        requirements=stop.get('driver_instructions') or None,
                        note=stop.get('note') or None,
                    )

                ratecon = request.FILES.get('ratecon')
                if ratecon:
                    LoadFile.objects.create(load=load, name='RateCon', file=ratecon)

                LoadHistory.objects.create(
                    load=load,
                    changed_by=booked_by,
                    description=payload.get('history_note') or 'Booked automatically from a verified rate confirmation.',
                )
        except Exception as e:
            logger.error("internal book-load failed | company=%s | error=%s", company_id, e)
            return Response(
                {"error": f"Failed to create load: {e}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({
            "load_id": load.id,
            "shipment": load.shipment,
            "load_number": load.load_number,
            "already_existed": False,
        }, status=status.HTTP_201_CREATED)


class InternalBusyDriversView(views.APIView):
    """
    Drivers currently running a load.

    The telegram bot skips these when matching, so a driver already on the road
    is not offered more freight. 'Dispatched' and 'In Transit' are the same
    statuses the driver app treats as active (see mobile.views.DriverActiveLoad).
    """
    permission_classes = [IsInternalService]

    ACTIVE_STATUSES = ['Dispatched', 'In Transit']

    def get(self, request):
        driver_ids = (
            Load.objects
            .filter(status__name__in=self.ACTIVE_STATUSES, driver__isnull=False)
            .values_list('driver_id', flat=True)
            .distinct()
        )
        return Response({
            "driver_ids": list(driver_ids),
            "active_statuses": self.ACTIVE_STATUSES,
        })
