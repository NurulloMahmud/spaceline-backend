from concurrent.futures import ThreadPoolExecutor
from typing import Any
from datetime import timedelta
import json
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
import copy
from django.db import transaction
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework import status, viewsets, views, generics, parsers
from rest_framework.decorators import action
from django.db.models import Count, F, Q
from django.http import QueryDict
from hiring.google_maps import geocode_zip
from hiring.haversine import haversine
from users.pagination import CustomPagination
from users.permissions import (
    IsInternalService,
)
from .utils import (
    build_change_description,
    DEPOSIT_FK_DISPLAY,
    DRIVER_COMPANY_FK_DISPLAY,
    DRIVER_FK_DISPLAY,
    VEHICLE_FK_DISPLAY,
)
from .pdf_generation import fill_w9, generate_contract
from .document_ai import MAX_FILES_PER_REQUEST, build_prefill, parse_document
from .team import NoVehicle, set_team
from . import board as hiring_board
from users.models import CustomUser, Company
from .models import (
    CompanyFile,
    Deposit,
    DepositHistory,
    Driver,
    DriverCompany,
    DriverFile,
    DriverHistory,
    DriverInviteLink,
    DriverStatus,
    Vehicle,
    VehicleEquipment,
    VehicleFile,
    CompanyHistory,
    VehicleHistory,
)
from .serializers import (
    DriverVehicleBlockSerializer,
    is_empty_block,
    CompanyFileSerializer,
    DepositHistoryViewSerializer,
    DepositHistoryWriteSerializer,
    DepositViewSerializer,
    DepositWriteSerializer,
    DriverBulkCreateSerializer,
    DriverCompanyModalSerializer,
    DriverFileSerializer,
    DriverHistoryViewSerializer,
    DriverHistoryWriteSerializer,
    DriverListSerializer,
    DriverViewSerializer,
    DriverWriteSerializer,
    DriverStatusSerializer,
    NearbyDriverSerializer,
    VehicleDropdownSerializer,
    VehicleFileSerializer,
    VehicleHistoryViewSerializer,
    VehicleHistoryWriteSerializer,
    VehicleViewSerializer,
    VehicleWriteSerializer,
    TrackingDriverSerializer,
    DriverCompanyViewSerializer,
    DriverCompanyWriteSerializer,
    CompanyHistoryWriteSerializer,
    CompanyHistoryViewSerializer,
)


class DriverStatusViewSet(viewsets.ModelViewSet):
    queryset = DriverStatus.objects.all().order_by("id")
    serializer_class = DriverStatusSerializer
    permission_classes = [IsAuthenticated]


class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    permission_classes = [IsAuthenticated | IsInternalService]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["status", "telegram_group_id", "team", "dispatcher"]
    search_fields = [
        "full_name",
        "ssn",
        "phone_number",
        "email",
        "driver_id",
        "address",
        "unit_number",
    ]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return DriverWriteSerializer
        else:
            return DriverViewSerializer

    def get_queryset(self):
        if self.request.headers.get("X-Internal-Secret"):
            qs = Driver.objects.all()
            telegram_group_id = self.request.query_params.get("telegram_group_id")
            if telegram_group_id:
                qs = qs.filter(telegram_group_id=telegram_group_id)
            return qs
        return Driver.objects.all()

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Quick create; an optional `vehicle` block creates the driver's vehicle too."""
        vehicle_block = _vehicle_block_serializer(request.data, driver=None)
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            self.perform_create(serializer)
            if vehicle_block is not None:
                _create_vehicle(
                    vehicle_block, serializer.instance, request.user, "with the driver"
                )
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        if self.request.user.department.name.lower() in [
            "management",
            "developer",
            "billing",
            "payroll",
        ]:
            serializer.save()
        else:
            serializer.save(referral_by=self.request.user)

    def partial_update(self, request, *args, **kwargs):
        from .google_maps import geocode_zip

        is_internal = bool(request.headers.get("X-Internal-Secret"))
        driver = self.get_object()
        old_instance = copy.deepcopy(driver)
        old_instance.status = driver.status
        old_instance.manager = driver.manager
        old_instance.referral_by = driver.referral_by
        old_instance.driver_company = driver.driver_company
        # Edit driver may add the vehicle a driver was created without.
        vehicle_block = _vehicle_block_serializer(request.data, driver=driver)
        serializer = self.get_serializer(driver, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            updated_driver = serializer.save()
            if vehicle_block is not None:
                _create_vehicle(
                    vehicle_block, updated_driver, request.user, "from Edit driver"
                )
        updated_driver.refresh_from_db()
        updated_driver.status
        updated_driver.manager
        updated_driver.referral_by
        updated_driver.driver_company
        if "current_zip" in request.data and request.data["current_zip"]:
            geo = geocode_zip(request.data["current_zip"])
            if geo:
                Driver.objects.filter(pk=updated_driver.pk).update(
                    current_city=geo["city"],
                    current_state=geo["state"],
                    current_address=geo["address"],
                    current_longitude=geo["lng"],
                    current_latitude=geo["lat"],
                )
                updated_driver.refresh_from_db()
        if any(
            k in request.data
            for k in ("current_zip", "current_latitude", "current_longitude")
        ):
            Driver.objects.filter(pk=updated_driver.pk).update(
                current_location_updated_at=timezone.now(),
            )
            updated_driver.refresh_from_db()
        if not is_internal:
            description = build_change_description(
                old_instance,
                updated_driver,
                [k for k in request.data.keys() if k != "vehicle"],
                DRIVER_FK_DISPLAY,
            )
            if description:
                DriverHistory.objects.create(
                    driver=updated_driver,
                    changed_by=request.user,
                    description=description,
                )
        return Response(DriverViewSerializer(updated_driver).data)

    def destroy(self, request, *args, **kwargs):
        """Deletes the driver, or, when they have loads, payouts, expenses or
        statements, sets them Out of service and answers 200 with
        `{"outcome": "out_of_service", "driver": ...}`."""
        driver = self.get_object()
        outcome = hiring_board.remove_driver(driver, request.user)
        if outcome == "deleted":
            return Response(status=status.HTTP_204_NO_CONTENT)
        driver.refresh_from_db()
        return Response({"outcome": outcome, "driver": DriverViewSerializer(driver).data})

    @action(
        detail=True,
        methods=["post"],
        url_path="hiring-stage",
        permission_classes=[IsAuthenticated],
    )
    def hiring_stage(self, request: Request, pk: str | None = None) -> Response:
        """POST /hiring/drivers/{id}/hiring-stage/ {stage}: move on the hiring board.

        stage is lead, called, ready or active; the status follows (Active for
        active, Pending otherwise). Answers the driver's board card."""
        driver = self.get_object()
        stage = str((request.data or {}).get("stage", "")).strip().lower()
        hiring_board.move(driver, stage, request.user)
        fresh = hiring_board.board_queryset().filter(pk=driver.pk).first()
        if fresh is None:
            fresh = hiring_board.with_records(Driver.objects.filter(pk=driver.pk)).select_related(
                "status", "referral_by", "hiring_moved_by"
            ).first()
        return Response(hiring_board.card(fresh, request))

    @action(
        detail=True,
        methods=["post"],
        url_path="team",
        permission_classes=[IsAuthenticated],
    )
    def team(self, request: Request, pk: str | None = None) -> Response:
        """
        POST /hiring/drivers/{id}/team/ {second_driver_id}: set or clear the second driver.

        Sets `second_driver` on this driver's first vehicle; null clears it.
        The picked driver is taken off any other truck (`moved_from`). See
        DRIVER_TEAM_API.md.
        """
        driver = self.get_object()
        body = request.data if hasattr(request.data, "get") else {}
        if "second_driver_id" not in body:
            return Response(
                {"detail": "second_driver_id is required (null clears).", "code": "invalid"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        raw = body.get("second_driver_id")
        second = None
        if raw not in (None, ""):
            try:
                second_id = int(raw)
            except (TypeError, ValueError):
                return Response(
                    {
                        "detail": "second_driver_id is required (null clears).",
                        "code": "invalid",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if second_id == driver.id:
                return Response(
                    {
                        "detail": "A driver cannot be their own second driver.",
                        "code": "same_driver",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            second = get_object_or_404(self.get_queryset(), pk=second_id)
        try:
            result = set_team(driver, second, request.user)
        except NoVehicle:
            return Response(
                {"detail": "Add a vehicle to this driver first.", "code": "no_vehicle"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(result)

    @action(
        detail=True,
        methods=["post"],
        url_path="invite-app",
        permission_classes=[IsAuthenticated],
    )
    def invite_app(self, request, pk=None):
        """
        Invite the driver to the driver app through the Driver Hub.

        Body may carry `email` to correct the address first. The hub answers
        with the invite id and whether the driver already works with us
        (`status: active`), both kept on the driver so the Drivers page can
        show Connected / Invited / Declined.

        Invites go only to drivers with a driver-app account: for any other
        email the hub answers 409 `not_registered`, passed on as is, and
        nothing on the driver changes.
        """
        from hub.payouts import push_driver_payouts_later
        from hub.registry import forget
        from hub.services import driver_hub

        driver = self.get_object()
        email = (request.data.get("email") or driver.email or "").strip()
        if not email:
            return Response(
                {"error": "The driver has no email address."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not driver_hub.configured():
            return Response(
                {"error": "The driver app is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        user = request.user
        invited_by = f"{user.first_name} {user.last_name}".strip() or user.username
        try:
            result = driver_hub.invite(email, invited_by) or {}
        except driver_hub.DriverHubError as exc:
            if exc.status_code == 409 and exc.code == driver_hub.NOT_REGISTERED:
                return Response(
                    {"error": exc.detail, "code": driver_hub.NOT_REGISTERED},
                    status=status.HTTP_409_CONFLICT,
                )
            code = (
                status.HTTP_502_BAD_GATEWAY
                if exc.status_code >= 500
                else status.HTTP_400_BAD_REQUEST
            )
            return Response(
                {"error": f"The driver app did not accept the invite: {exc.detail}"},
                status=code,
            )

        linked_before = driver.hub_driver_id
        driver.email = email
        driver.hub_invite_id = result.get("invite_id") or driver.hub_invite_id
        driver.hub_invite_status = result.get("status") or "pending"
        driver.hub_invited_at = timezone.now()
        hub_driver_id = result.get("hub_driver_id")
        if driver.hub_invite_status == "active" and hub_driver_id:
            if (
                not Driver.objects.filter(hub_driver_id=hub_driver_id)
                .exclude(pk=driver.pk)
                .exists()
            ):
                driver.hub_driver_id = hub_driver_id
                try:
                    driver_hub.link_driver(hub_driver_id, driver.id)
                except driver_hub.DriverHubError:
                    pass
        driver.save(
            update_fields=[
                "email",
                "hub_invite_id",
                "hub_invite_status",
                "hub_invited_at",
                "hub_driver_id",
            ]
        )
        if driver.hub_driver_id and driver.hub_driver_id != linked_before:
            # Linked at once: the app shows the payouts the driver has here.
            push_driver_payouts_later(driver.pk)
        DriverHistory.objects.create(
            driver=driver,
            changed_by=user,
            description=f"Invited to the driver app at {email}",
        )
        forget(email)
        return Response(
            {
                **result,
                "driver_id": driver.id,
                "email": email,
                "hub_status": driver.hub_status,
                "hub_invite_status": driver.hub_invite_status,
                "hub_invited_at": driver.hub_invited_at,
                "hub_driver_id": driver.hub_driver_id,
            }
        )


def _vehicle_block_serializer(
    data: Any, driver: Driver | None
) -> DriverVehicleBlockSerializer | None:
    """The validated optional `vehicle` block of a driver create or edit, or None.

    Empty or missing: None. Filled: VEHICLE_REQUIRED apply, and a driver who
    already has a vehicle is refused (Edit driver only adds a missing one).
    Raises ValidationError as {"vehicle": ...}.
    """
    block = nested_object(data, "vehicle")
    if is_empty_block(block):
        return None
    if driver is not None and driver.vehicles.exists():
        raise ValidationError(
            {"vehicle": ["This driver already has a vehicle. Edit it from the vehicle."]}
        )
    serializer = DriverVehicleBlockSerializer(data=block)
    if not serializer.is_valid():
        raise ValidationError({"vehicle": serializer.errors})
    return serializer


def _create_vehicle(
    block: DriverVehicleBlockSerializer, driver: Driver, user: Any, why: str
) -> Vehicle:
    vehicle = block.create_for(driver)
    if getattr(user, "is_authenticated", False):
        VehicleHistory.objects.create(
            vehicle=vehicle, changed_by=user, description=f"Vehicle added {why}"
        )
    return vehicle


class DriverFileViewSet(viewsets.ModelViewSet):
    serializer_class = DriverFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = DriverFile.objects.select_related("driver")
        driver_id = self.request.query_params.get("driver")
        if driver_id:
            qs = qs.filter(driver_id=driver_id)
        return qs


class CompanyFileViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = CompanyFile.objects.select_related("company")
        company_id = self.request.query_params.get("company")
        if company_id:
            qs = qs.filter(company_id=company_id)
        return qs


class VehicleFileViewSet(viewsets.ModelViewSet):
    serializer_class = VehicleFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = VehicleFile.objects.select_related("vehicle")
        vehicle_id = self.request.query_params.get("vehicle")
        if vehicle_id:
            qs = qs.filter(vehicle_id=vehicle_id)
        return qs


class VehicleViewSet(viewsets.ModelViewSet):
    queryset = Vehicle.objects.all()
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["driver__status"]
    search_fields = ["driver__full_name", "make", "model", "vin", "note"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return VehicleWriteSerializer
        else:
            return VehicleViewSerializer

    def get_queryset(self):
        return Vehicle.objects.all()

    def partial_update(self, request, *args, **kwargs):
        vehicle = self.get_object()
        old_instance = copy.deepcopy(vehicle)
        old_instance.driver = vehicle.driver
        old_instance.second_driver = vehicle.second_driver
        serializer = self.get_serializer(vehicle, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            updated_vehicle = serializer.save()
            # Equipment is not a Vehicle column, so the serializer drops it. The
            # edit dialog sends the whole list as `equipment_names`, which
            # replaces the rows. Not `equipment`: older copies of the dialog
            # sent `equipment: []` on every save without meaning to clear it.
            if "equipment_names" in request.data:
                replace_vehicle_equipment(
                    updated_vehicle, form_list(request.data, "equipment_names")
                )
        updated_vehicle.refresh_from_db()
        updated_vehicle.driver
        updated_vehicle.second_driver
        description = build_change_description(
            old_instance,
            updated_vehicle,
            list(request.data.keys()),
            VEHICLE_FK_DISPLAY,
        )
        if description:
            VehicleHistory.objects.create(
                vehicle=updated_vehicle,
                changed_by=request.user,
                description=description,
            )
        return Response(VehicleViewSerializer(updated_vehicle).data)


class DriverCompanyViewSet(viewsets.ModelViewSet):
    queryset = DriverCompany.objects.all()
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["drivers"]
    search_fields = ["name", "mc", "business_type"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return DriverCompanyWriteSerializer
        else:
            return DriverCompanyViewSerializer

    def get_queryset(self):
        return DriverCompany.objects.all()

    def partial_update(self, request, *args, **kwargs):
        driver_company = self.get_object()
        old_instance = copy.deepcopy(driver_company)
        serializer = self.get_serializer(
            driver_company, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        updated_company = serializer.save()
        updated_company.refresh_from_db()
        description = build_change_description(
            old_instance,
            updated_company,
            list(request.data.keys()),
            DRIVER_COMPANY_FK_DISPLAY,
        )
        if description:
            CompanyHistory.objects.create(
                company=updated_company,
                changed_by=request.user,
                description=description,
            )
        return Response(DriverCompanyViewSerializer(updated_company).data)

    def perform_destroy(self, instance):
        # A shared company's files/history would otherwise cascade-delete
        # for every attached driver, not just one — require detaching them
        # first so that's never an accident.
        if instance.drivers.exists():
            raise ValidationError(
                "This company still has drivers attached. Detach them before deleting it."
            )
        instance.delete()


class DepositViewSet(viewsets.ModelViewSet):
    queryset = Deposit.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["driver"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return DepositWriteSerializer
        else:
            return DepositViewSerializer

    def get_queryset(self):
        return Deposit.objects.all()

    def partial_update(self, request, *args, **kwargs):
        deposit = self.get_object()
        old_instance = copy.deepcopy(deposit)
        old_instance.driver = deposit.driver
        serializer = self.get_serializer(deposit, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_deposit = serializer.save()
        updated_deposit.refresh_from_db()
        updated_deposit.driver
        description = build_change_description(
            old_instance,
            updated_deposit,
            list(request.data.keys()),
            DEPOSIT_FK_DISPLAY,
        )
        if description:
            DepositHistory.objects.create(
                deposit=updated_deposit,
                changed_by=request.user,
                description=description,
            )
        return Response(DepositViewSerializer(updated_deposit).data)


class HiringBoardView(views.APIView):
    """GET /hiring/board/: the hiring board's four columns, newest first."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        return Response(hiring_board.board(request))


class DriverListView(generics.ListAPIView):
    serializer_class = DriverListSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    ALLOWED_SORT_FIELDS = {
        "created_at",
        "full_name",
        "hired_date",
        "terminated_date",
        "dob",
    }

    def get_queryset(self):
        qs = Driver.objects.all()

        status_id = self.request.query_params.get("status")
        search = self.request.query_params.get("search")
        manager_id = self.request.query_params.get("manager")
        if manager_id:
            qs = qs.filter(manager_id=manager_id)
        if status_id:
            qs = qs.filter(status_id=status_id)
        if search:
            qs = qs.filter(
                Q(full_name__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(email__icontains=search)
            )

        sort = self.request.query_params.get("sort", "-created_at")
        sort_field = sort.lstrip("-")
        if sort_field in self.ALLOWED_SORT_FIELDS:
            qs = qs.order_by(sort)
        else:
            qs = qs.order_by("-created_at")
        return hiring_board.with_records(qs).select_related("status", "manager")


class DriverCompanyModalView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        driver_id = request.query_params.get("driver")
        if not driver_id:
            return Response({"detail": "driver query param is required."}, status=400)

        driver = get_object_or_404(Driver, pk=driver_id)
        # Imported and quick-created drivers have no company yet, but they are
        # still on the list and have to open.
        if driver.driver_company_id is None:
            return Response(DriverCompanyModalSerializer.without_company(driver))

        company = get_object_or_404(
            DriverCompany.objects.prefetch_related(
                "drivers__status",
                "drivers__manager",
                "companyfile_set",
            ),
            pk=driver.driver_company_id,
        )
        serializer = DriverCompanyModalSerializer(company, context={"driver": driver})
        return Response(serializer.data)


class GenerateInviteLinkView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        manager_id = request.data.get("manager")
        manager = None
        if manager_id:
            manager = get_object_or_404(CustomUser, id=manager_id)

        invite = DriverInviteLink.objects.create(
            created_by=request.user,
            manager=manager,
            expires_at=timezone.now() + timedelta(days=7),
        )
        link = f"https://spaceline.boxtruckmanage.com/driver-form/?token={invite.token}"
        return Response({"link": link}, status=201)


SIGN_FILE_NAMES = ("W-9 (Generated)", "Contractor Agreement (Generated)")


class GenerateDriverSignLinkView(views.APIView):
    """Staff-facing: mint a one-shot, unauthenticated link for an existing
    driver to review and sign their W-9 and contractor agreement."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        driver_id = request.data.get("driver_id")
        if not driver_id:
            return Response({"detail": "driver_id is required."}, status=400)

        driver = get_object_or_404(Driver, id=driver_id)

        invite = DriverInviteLink.objects.create(
            created_by=request.user,
            driver=driver,
            expires_at=timezone.now() + timedelta(days=7),
        )
        link = f"https://spaceline.boxtruckmanage.com/driver-sign/?token={invite.token}"
        return Response({"link": link}, status=201)


class DriverSignInfoView(views.APIView):
    """Public, token-based. Curated driver/vehicle/company info plus the
    W-9 and contract files, for the signing page — deliberately excludes
    SSN and banking (Deposit) fields since this endpoint carries no auth."""

    permission_classes = []

    def get(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Token is required."}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related("driver").get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({"detail": "Invalid token."}, status=400)

        if not invite.is_valid():
            return Response({"detail": "Link has expired or is inactive."}, status=400)

        driver = invite.driver
        if not driver:
            return Response(
                {"detail": "This link is not a document-signing link."}, status=400
            )

        vehicle = driver.vehicles.first()
        driver_company = driver.driver_company
        sign_files = DriverFile.objects.filter(driver=driver, name__in=SIGN_FILE_NAMES)

        return Response(
            {
                "driver": {
                    "id": driver.id,
                    "full_name": driver.full_name,
                    "phone_number": driver.phone_number,
                    "email": driver.email,
                    "address": driver.address,
                    "unit_number": driver.unit_number,
                    "city": driver.city,
                    "state": driver.state,
                    "zip_code": driver.zip_code,
                },
                "vehicle": (
                    {
                        "id": vehicle.id,
                        "vehicle_type": vehicle.vehicle_type,
                        "make": vehicle.make,
                        "model": vehicle.model,
                        "year": vehicle.year,
                    }
                    if vehicle
                    else None
                ),
                "company": (
                    {
                        "name": driver_company.name,
                        "mc": driver_company.mc,
                        "address": driver_company.address,
                        "city": driver_company.city,
                        "state": driver_company.state,
                        "zipcode": driver_company.zipcode,
                    }
                    if driver_company
                    else None
                ),
                "files": [
                    {"id": f.id, "name": f.name, "url": f.document.url}
                    for f in sign_files
                ],
            }
        )


def mutable_request_data(data):
    copied = QueryDict("", mutable=True)
    for key in data:
        copied.setlist(key, data.getlist(key))
    return copied


# Uploads on the invite endpoints: a repeated file key paired with a repeated
# name key, one pair per record the files hang off. Same names the multipart
# DriverBulkCreateSerializer already uses, so a frontend that moves between
# the two flows keeps the same field names.
INVITE_FILE_GROUPS = (
    ("driver", "driver_files", "driver_file_names"),
    ("company", "company_files", "company_file_names"),
    ("vehicle", "vehicle_files", "vehicle_file_names"),
)


def form_list(data, key):
    """Repeated keys: a list under multipart, already a list under JSON."""
    if hasattr(data, "getlist"):
        return data.getlist(key)
    value = data.get(key)
    if value in (None, ""):
        return []
    return value if isinstance(value, list) else [value]


def form_bool(value):
    """Multipart has no booleans — 'false' is a non-empty string, i.e. True."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def replace_vehicle_equipment(vehicle, names):
    """Make the vehicle's equipment exactly `names` (blank entries dropped)."""
    wanted = []
    for name in names:
        name = str(name or "").strip()[:255]
        if name and name not in wanted:
            wanted.append(name)
    current = list(
        VehicleEquipment.objects.filter(vehicle=vehicle)
        .order_by("id")
        .values_list("name", flat=True)
    )
    if current == wanted:
        return
    VehicleEquipment.objects.filter(vehicle=vehicle).delete()
    VehicleEquipment.objects.bulk_create(
        [VehicleEquipment(vehicle=vehicle, name=name) for name in wanted]
    )


def invite_registered_driver(token):
    """The driver an invite link registered itself, while it may still edit.

    None for a missing, unknown, inactive or expired link, and for a sign
    link (its driver is older than the link).
    """
    if not token:
        return None
    try:
        invite = DriverInviteLink.objects.select_related("driver").get(token=token)
    except (DriverInviteLink.DoesNotExist, ValueError, DjangoValidationError):
        return None
    driver = invite.driver
    if driver is None or not invite.is_valid():
        return None
    if driver.created_at < invite.created_at:
        return None
    return driver


def invite_dock_height(vehicle_data, data):
    """Dock height in inches and dock type in the one `dock_height` column.

    Written the way the driver app sync writes it ("48 in Dock high"), so
    both sources read the same on the vehicle.
    """
    inches = "".join(
        ch for ch in str(vehicle_data.get("dock_height") or "") if ch.isdigit()
    )
    dock = vehicle_data.get("dock") or form_list(data, "dock")
    if isinstance(dock, list):
        dock = ", ".join(str(d) for d in dock if d)
    parts = [f"{int(inches)} in" if inches and int(inches) else "", str(dock or "")]
    return " ".join(p for p in parts if p)


def nested_object(data, key):
    """`driver`/`vehicle` arrive as objects in JSON, JSON strings in multipart."""
    value = data.get(key)
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def collect_invite_uploads(request):
    """Pair each file group with its names, or return the first count mismatch.

    Returns (uploads, error) — `uploads` maps the group name to a list of
    (file, name) pairs, so the caller can build rows once the records the
    files attach to exist.
    """
    uploads = {}
    for group, files_key, names_key in INVITE_FILE_GROUPS:
        files = request.FILES.getlist(files_key)
        names = [str(name) for name in form_list(request.data, names_key)]
        if len(files) != len(names):
            return None, f"{files_key} and {names_key} must have the same count."
        uploads[group] = list(zip(files, names))
    return uploads, None


class DriverBulkCreateHRView(views.APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        # if request.user.department.name.lower() not in ['management', 'payroll', 'billing', 'hiring']:
        #     return Response({'detail': 'Not allowed.'}, status=403)

        data = mutable_request_data(request.data)
        pending_status = get_object_or_404(DriverStatus, name__iexact="pending")
        data["status"] = pending_status.id
        data["referral_by"] = request.user.id
        serializer = DriverBulkCreateSerializer(data=data)
        if serializer.is_valid():
            driver = serializer.save()
            return Response(
                {"detail": "Driver created.", "driver_id": driver.id}, status=201
            )
        return Response(serializer.errors, status=400)


class DriverBulkCreateInviteView(views.APIView):
    permission_classes = []
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Token is required."}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related("created_by").get(
                token=token
            )
        except DriverInviteLink.DoesNotExist:
            return Response({"detail": "Invalid token."}, status=400)

        if not invite.is_valid():
            return Response({"detail": "Link has expired or is inactive."}, status=400)
        data = mutable_request_data(request.data)
        # driver_company links to an existing business by internal id — a
        # public applicant has no business knowing those ids, so this is a
        # staff-only capability (see DriverBulkCreateHRView) and is always
        # stripped here regardless of what's submitted.
        data.pop("driver_company", None)
        pending_status = get_object_or_404(DriverStatus, name__iexact="pending")
        data["status"] = pending_status.id
        data["referral_by"] = invite.created_by.id
        if invite.manager_id:
            data["manager"] = invite.manager_id
        serializer = DriverBulkCreateSerializer(data=data)
        if serializer.is_valid():
            driver = serializer.save()
            invite.is_active = False
            invite.save(update_fields=["is_active"])
            return Response(
                {"detail": "Driver created.", "driver_id": driver.id}, status=201
            )
        return Response(serializer.errors, status=400)


class DriverExistsCheckView(views.APIView):
    permission_classes = []

    def post(self, request):
        email = (request.data.get("email") or "").strip()
        phone_number = (request.data.get("phone_number") or "").strip()

        if not email and not phone_number:
            return Response(
                {"detail": "email or phone_number is required."}, status=400
            )

        # A driver sending the invite form again must not be warned about
        # their own record: the link they hold names it.
        own = invite_registered_driver(request.query_params.get("token"))
        drivers = Driver.objects.all()
        if own is not None:
            drivers = drivers.exclude(pk=own.pk)

        email_exists = False
        if email:
            email_exists = drivers.filter(email__iexact=email).exists()

        phone_exists = False
        if phone_number:
            phone_exists = drivers.filter(phone_number=phone_number).exists()

        return Response(
            {"email_exists": email_exists, "phone_exists": phone_exists}, status=200
        )


class DriverInviteSubmitView(views.APIView):
    """Public, token-based endpoint for the driver invite registration form.

    Unlike DriverBulkCreateInviteView (multipart, files included), the frontend
    for this flow sends plain JSON values only, with company/driver/vehicle
    fields mixed flat and nested (some duplicated at both levels). Rather than
    fight DRF's nested-serializer validation for that shape, fields are pulled
    defensively with `_pick`. On success this also generates a W-9 and the
    inviting company's contract PDF and returns their URLs.

    Files are optional here. Sent as multipart, the three
    `<record>_files` / `<record>_file_names` pairs land on the driver, the
    driver's company and the vehicle respectively, so a registration card
    stays on the vehicle instead of being flattened onto the driver. Under
    multipart the `driver` and `vehicle` objects come across as JSON strings;
    a plain JSON body (no files) keeps working exactly as before.
    """

    permission_classes = []
    parser_classes = [parsers.JSONParser, parsers.MultiPartParser, parsers.FormParser]

    def get(self, request):
        """Whether the form can still be filled, asked before the driver types.

        `open`: nobody has registered with it yet. `registered`: this link
        registered a driver who has not signed yet; sending the form again
        updates them. `closed`: used up, expired, or a sign link.
        """
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Token is required."}, status=400)
        try:
            invite = DriverInviteLink.objects.select_related("driver").get(
                token=token
            )
        except (DriverInviteLink.DoesNotExist, ValueError, DjangoValidationError):
            return Response({"detail": "Invalid token."}, status=400)
        if not invite.is_valid():
            state = "closed"
        elif invite.driver is None:
            state = "open"
        elif invite.driver.created_at >= invite.created_at:
            state = "registered"
        else:
            state = "closed"
        return Response({"state": state})

    def post(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Token is required."}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related("created_by").get(
                token=token
            )
        except DriverInviteLink.DoesNotExist:
            return Response({"detail": "Invalid token."}, status=400)

        if not invite.is_valid():
            return Response({"detail": "Link has expired or is inactive."}, status=400)

        # A sign-link is minted for a driver that already existed, so that
        # driver is older than the link and the link can never register.
        # A driver this link registered itself is newer than the link: until
        # the signed documents are in, sending the form again (the driver went
        # back to fix something, or reloaded the page) updates that same
        # driver instead of creating a second one or refusing.
        claimed = invite.driver
        if claimed is not None and claimed.created_at < invite.created_at:
            return Response(
                {"detail": "This link has already been used to register a driver."},
                status=409,
            )

        company = Company.current()
        if not company.contract_template_text:
            return Response(
                {
                    "detail": f"Company '{company.name}' has no contract_template_text configured."
                },
                status=422,
            )

        data = request.data
        driver_data = nested_object(data, "driver")
        vehicle_data = nested_object(data, "vehicle")

        # Paired up before anything is created, so a miscounted upload is a
        # plain 400 rather than a driver saved with half its documents.
        uploads, upload_error = collect_invite_uploads(request)
        if upload_error:
            return Response({"detail": upload_error}, status=400)

        def pick(*sources_and_keys, default=""):
            for source, key in sources_and_keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
            return default

        full_name = pick((driver_data, "driver_full_name"), (data, "driver_full_name"))
        phone_number = pick((driver_data, "phone"), (data, "phone"))
        company_name = data.get("company_name") or ""

        if not full_name or not company_name:
            return Response(
                {"detail": "driver_full_name and company_name are required."},
                status=400,
            )

        same_phone = Driver.objects.filter(phone_number=phone_number)
        if claimed is not None:
            same_phone = same_phone.exclude(pk=claimed.pk)
        if phone_number and same_phone.exists():
            return Response(
                {"detail": "A driver with this phone number already exists."},
                status=400,
            )

        pending_status = get_object_or_404(DriverStatus, name__iexact="pending")

        dock_height = invite_dock_height(vehicle_data, data)

        equipment = vehicle_data.get("equipment") or form_list(data, "equipment")
        if not isinstance(equipment, list):
            equipment = [equipment]

        # The form has one email, the owner's. It belongs on the driver (what
        # the onboarding page, the edit dialogs and the contract read) as well
        # as on their company.
        email = (
            pick((driver_data, "email"), (data, "company_email"), (data, "email"))
            or None
        )

        driver_fields = {
            "full_name": full_name,
            "phone_number": phone_number,
            "email": email,
            "emergency_phone_number": data.get("company_emergency_phone") or "",
            "address": data.get("company_address") or "",
            "city": data.get("company_city") or "",
            "state": data.get("company_state") or "",
            "zip_code": pick((data, "company_zip"), (data, "company_zipcode")),
            "tax_exempt": form_bool(data.get("tax_exempt")),
            "payee_code": data.get("payee_code") or "",
            "fatca_reporting_code": data.get("fatca_reporting_code") or "",
        }
        company_fields = {
            "name": company_name,
            "mc": data.get("company_mc") or "",
            "employer_id": data.get("company_employer_id") or "",
            "phone_number": data.get("company_phone") or "",
            "business_as": data.get("company_doing_business") or "",
            "business_type": str(data.get("company_type") or ""),
            "zipcode": pick((data, "company_zip"), (data, "company_zipcode")),
            "state": data.get("company_state") or "",
            "city": data.get("company_city") or "",
            "address": data.get("company_address") or "",
            "email": data.get("company_email") or None,
            "applicant_first_name": data.get("company_applicant_first_name") or "",
            "applicant_last_name": data.get("company_applicant_last_name") or "",
        }
        # The vehicle is optional (SPEC-1.4 2.4): nothing filled, no Vehicle.
        # Vehicle files need one to hang on, so they still make one.
        vehicle_filled = not is_empty_block(
            {**vehicle_data, "dock": dock_height, "equipment": equipment}
        )
        vehicle_fields = {
            "make": vehicle_data.get("make") or "",
            "model": vehicle_data.get("model") or "",
            "length": vehicle_data.get("useful_cargo_length") or None,
            "width": vehicle_data.get("useful_cargo_width") or None,
            "height": vehicle_data.get("useful_cargo_height") or None,
            "gvw": vehicle_data.get("GVW_lbs") or 0,
            "payload": vehicle_data.get("payload_lbs") or 0,
            "door_open_width": vehicle_data.get("door_width") or None,
            "door_open_height": vehicle_data.get("door_height") or None,
            "dock_height": dock_height,
        }

        with transaction.atomic():
            if claimed is None:
                driver = Driver.objects.create(
                    status=pending_status,
                    referral_by=invite.created_by,
                    manager=invite.manager,
                    **driver_fields,
                )
                driver_company = DriverCompany.objects.create(**company_fields)
                driver.driver_company = driver_company
                driver.save(update_fields=["driver_company"])
                vehicle = (
                    Vehicle.objects.create(driver=driver, **vehicle_fields)
                    if vehicle_filled or uploads["vehicle"]
                    else None
                )
            else:
                driver = claimed
                for key, value in driver_fields.items():
                    setattr(driver, key, value)
                driver_company = driver.driver_company or DriverCompany()
                for key, value in company_fields.items():
                    setattr(driver_company, key, value)
                driver_company.save()
                driver.driver_company = driver_company
                driver.save()
                vehicle = driver.vehicles.order_by("id").first()
                if vehicle_filled:
                    vehicle = vehicle or Vehicle(driver=driver)
                    for key, value in vehicle_fields.items():
                        setattr(vehicle, key, value)
                    vehicle.save()
                    VehicleEquipment.objects.filter(vehicle=vehicle).delete()
                elif vehicle is None and uploads["vehicle"]:
                    # Vehicle files alone: a blank vehicle to hang them on.
                    vehicle = Vehicle.objects.create(driver=driver, **vehicle_fields)
                # An empty vehicle block leaves a vehicle the first submit
                # made exactly as it is (SPEC-1.4 2.4: the vehicle is optional).
                # The PDFs are generated from the answers, so new answers mean
                # new PDFs; the old pair would otherwise sit next to them.
                DriverFile.objects.filter(
                    driver=driver, name__in=SIGN_FILE_NAMES
                ).delete()

            if vehicle is not None and vehicle_filled:
                VehicleEquipment.objects.bulk_create(
                    [
                        VehicleEquipment(vehicle=vehicle, name=str(name))
                        for name in equipment
                        if name
                    ]
                )

            uploaded_driver_files = DriverFile.objects.bulk_create(
                [
                    DriverFile(driver=driver, name=name, document=file)
                    for file, name in uploads["driver"]
                ]
            )
            uploaded_company_files = CompanyFile.objects.bulk_create(
                [
                    CompanyFile(company=driver_company, name=name, document=file)
                    for file, name in uploads["company"]
                ]
            )
            uploaded_vehicle_files = (
                VehicleFile.objects.bulk_create(
                    [
                        VehicleFile(vehicle=vehicle, name=name, document=file)
                        for file, name in uploads["vehicle"]
                    ]
                )
                if vehicle is not None
                else []
            )

            w9_bytes = fill_w9(
                {
                    "company_name": company_name,
                    "company_doing_business": data.get("company_doing_business") or "",
                    "company_address": data.get("company_address") or "",
                    "company_city": data.get("company_city") or "",
                    "company_state": data.get("company_state") or "",
                    "company_zip": driver_company.zipcode,
                    "company_employer_id": data.get("company_employer_id") or "",
                    "company_type": data.get("company_type") or "",
                    "payee_code": driver.payee_code,
                    "fatca_reporting_code": driver.fatca_reporting_code,
                }
            ).getvalue()

            contractor_address = ", ".join(
                part
                for part in [
                    data.get("company_address"),
                    data.get("company_city"),
                    data.get("company_state"),
                    driver_company.zipcode,
                ]
                if part
            )
            today = timezone.now()
            contract_bytes = generate_contract(
                company,
                {
                    "effective_day": today.strftime("%d"),
                    "effective_month": today.strftime("%B"),
                    "effective_year": today.strftime("%y"),
                    "contractor_name": company_name,
                    "contractor_address": contractor_address,
                    "contractor_email": data.get("company_email") or "",
                },
            ).getvalue()
            w9_file = DriverFile(driver=driver, name="W-9 (Generated)")
            w9_file.document.save(
                f"w9_{driver.id}.pdf", ContentFile(w9_bytes), save=True
            )
            contract_file = DriverFile(
                driver=driver, name="Contractor Agreement (Generated)"
            )
            contract_file.document.save(
                f"contract_{driver.id}.pdf", ContentFile(contract_bytes), save=True
            )

            # The link deliberately stays active. Registration is only half the
            # flow: the driver still has to sign the two PDFs generated above
            # and post them back to /driver/invite/documents/, which
            # authenticates with this same token. Deactivating here left every
            # driver unable to return their signed W-9 and contract.
            #
            # Claiming the invite with the driver it just created ties the
            # link to that driver: a second submit updates it, and the upload
            # endpoint is what finally deactivates the link.
            if claimed is None:
                invite.driver = driver
                invite.save(update_fields=["driver"])

        def described(files):
            return [{"id": f.id, "name": f.name, "url": f.document.url} for f in files]

        return Response(
            {
                "detail": "Driver created.",
                "driver_id": driver.id,
                "vehicle_id": vehicle.id if vehicle is not None else None,
                "w9_url": w9_file.document.url,
                "contract_url": contract_file.document.url,
                "files": {
                    "driver": described(uploaded_driver_files),
                    "company": described(uploaded_company_files),
                    "vehicle": described(uploaded_vehicle_files),
                },
            },
            status=201,
        )


class DriverInviteDocumentUploadView(views.APIView):
    permission_classes = []
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        token = request.query_params.get("token")
        if not token:
            return Response({"detail": "Token is required."}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related("driver").get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({"detail": "Invalid token."}, status=400)

        if not invite.is_valid():
            return Response({"detail": "Link has expired or is inactive."}, status=400)

        # The driver comes from the invite, not the request body. This endpoint
        # is unauthenticated, so a token holder who could name any driver_id
        # could attach files to any driver in the company.
        driver = invite.driver
        if driver is None:
            return Response(
                {"detail": "Submit the registration form before uploading documents."},
                status=409,
            )

        # driver_id is still accepted for the documented request shape, but it
        # only ever confirms what the token already decided.
        requested_id = request.data.get("driver_id")
        if requested_id not in (None, "") and str(requested_id) != str(driver.id):
            return Response(
                {"detail": "driver_id does not match this link."}, status=400
            )

        files = request.FILES.getlist("files")
        names = (
            request.data.getlist("names")
            if hasattr(request.data, "getlist")
            else request.data.get("names", [])
        )
        if len(files) != len(names):
            return Response(
                {"detail": "files and names must have the same count."}, status=400
            )

        created = DriverFile.objects.bulk_create(
            [
                DriverFile(driver=driver, name=name, document=file)
                for file, name in zip(files, names)
            ]
        )

        invite.is_active = False
        invite.save(update_fields=["is_active"])

        return Response(
            {
                "detail": "Documents uploaded.",
                "files": [
                    {"id": f.id, "name": f.name, "url": f.document.url} for f in created
                ],
            },
            status=201,
        )


class DriverDocumentParseView(views.APIView):
    """Read a batch of onboarding documents with AI and return form values.

    Staff uploads whatever the driver sent — license, registration, COI, W-9,
    a voided check — and gets back the fields printed on them, grouped by the
    form section they belong to, ready to prefill the driver-creation form.

    This endpoint is read-only by design: nothing is saved, no Driver is
    created, no file is stored. It is a suggestion for a human to confirm,
    and the existing create/update endpoints are what persist anything. That
    also means a bad extraction costs nothing but a retry.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        files = request.FILES.getlist("files") or request.FILES.getlist("file")
        if not files:
            return Response({"detail": "At least one file is required."}, status=400)
        if len(files) > MAX_FILES_PER_REQUEST:
            return Response(
                {"detail": f"At most {MAX_FILES_PER_REQUEST} files per request."},
                status=400,
            )

        # Optional per-file hints, positional against `files`. Fewer hints than
        # files is fine — the model identifies the rest on its own.
        hints = (
            request.data.getlist("document_types")
            if hasattr(request.data, "getlist")
            else []
        )
        hints += [None] * (len(files) - len(hints))

        # Documents are read concurrently: a batch of five otherwise costs five
        # round trips end to end, which is the difference between a recruiter
        # waiting and a recruiter walking away.
        with ThreadPoolExecutor(
            max_workers=min(len(files), MAX_FILES_PER_REQUEST)
        ) as pool:
            results = list(
                pool.map(
                    lambda pair: parse_document(pair[0], pair[1] or None),
                    zip(files, hints[: len(files)]),
                )
            )

        prefill, conflicts, sensitive_fields = build_prefill(
            [r for r in results if not r["error"]]
        )

        return Response(
            {
                "results": results,
                "prefill": prefill,
                "conflicts": conflicts,
                "sensitive_fields": sensitive_fields,
            },
            status=200,
        )


class CompanyHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["company"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return CompanyHistoryWriteSerializer
        else:
            return CompanyHistoryViewSerializer

    def get_queryset(self):
        return CompanyHistory.objects.select_related("changed_by", "company").all()


class DriverHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["driver"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return DriverHistoryWriteSerializer
        else:
            return DriverHistoryViewSerializer

    def get_queryset(self):
        return DriverHistory.objects.select_related("changed_by", "driver").all()


class VehicleHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["vehicle"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return VehicleHistoryWriteSerializer
        else:
            return VehicleHistoryViewSerializer

    def get_queryset(self):
        return VehicleHistory.objects.select_related("changed_by", "vehicle").all()


class DepositHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["deposit"]

    def get_serializer_class(self):
        if self.request.method in ["POST", "PATCH"]:
            return DepositHistoryWriteSerializer
        else:
            return DepositHistoryViewSerializer

    def get_queryset(self):
        return DepositHistory.objects.select_related("changed_by", "deposit").all()


class DriverBulkView(views.APIView):
    permission_classes = [IsInternalService]

    def get(self, request):
        ids = request.query_params.get("ids", "")
        if not ids:
            return Response([])
        id_list = [i for i in ids.split(",") if i.strip().isdigit()]
        drivers = Driver.objects.filter(id__in=id_list).values("id", "full_name")
        return Response(list(drivers))


class DriverLocationsView(views.APIView):
    """Every active driver that has a position, for one company.

    Internal-only. atrek's load list calls this once per request and measures
    each load's pickup against the result itself, so it can show the trucks
    near every row without a geocoding round-trip per load (which is what
    drivers-nearby would cost).
    """

    permission_classes = [IsInternalService]

    def get(self, request):
        qs = Driver.objects.filter(
            status__name__iexact="active",
            current_latitude__isnull=False,
            current_longitude__isnull=False,
        ).prefetch_related("vehicles")

        company_id = Company.current().id
        drivers = []
        for driver in qs:
            vehicle = driver.vehicles.first()
            drivers.append(
                {
                    "id": driver.id,
                    "full_name": driver.full_name,
                    "unit_number": driver.unit_number,
                    "phone_number": driver.phone_number,
                    "company_id": company_id,
                    "current_latitude": driver.current_latitude,
                    "current_longitude": driver.current_longitude,
                    "current_city": driver.current_city,
                    "current_state": driver.current_state,
                    "current_zip": driver.current_zip,
                    "current_location_updated_at": driver.current_location_updated_at,
                    "available_at": driver.available_at,
                    "availability_note": driver.availability_note,
                    "vehicle_type": vehicle.vehicle_type if vehicle else None,
                }
            )
        return Response(drivers)


class DriverNearbyView(views.APIView):
    permission_classes = [IsAuthenticated | IsInternalService]

    def get(self, request):
        zip_code = request.query_params.get("zip")
        radius = float(request.query_params.get("radius", 100))
        if not zip_code:
            return Response({"detail": "zip is required."}, status=400)

        geo = geocode_zip(zip_code)
        if not geo:
            return Response(
                {"detail": "Could not geocode the provided zip code."}, status=400
            )

        pickup_lat = geo["lat"]
        pickup_lng = geo["lng"]

        qs = (
            Driver.objects.filter(
                status__name__iexact="active",
                current_latitude__isnull=False,
                current_longitude__isnull=False,
            )
            .select_related("status")
            .prefetch_related(
                "vehicles",
                "vehicles__vehicleequipment_set",
            )
        )

        nearby = []
        for driver in qs:
            miles = haversine(
                pickup_lat,
                pickup_lng,
                driver.current_latitude,
                driver.current_longitude,
            )
            if miles <= radius:
                driver._miles_out = miles
                nearby.append(driver)

        nearby.sort(key=lambda d: d._miles_out)
        serializer = NearbyDriverSerializer(nearby, many=True)
        return Response(
            {
                "pickup": {
                    "zip": zip_code,
                    "lat": pickup_lat,
                    "lng": pickup_lng,
                    "city": geo["city"],
                    "state": geo["state"],
                },
                "radius_miles": radius,
                "count": len(nearby),
                "drivers": serializer.data,
            }
        )


class VehicleDropdownView(generics.ListAPIView):
    serializer_class = VehicleDropdownSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Vehicle.objects.all()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        requested_date = self.request.query_params.get("requested_date")
        if requested_date:
            requested_date_parsed = parse_date(requested_date)
            if not requested_date_parsed:
                raise ValidationError(
                    {"error": "Invalid requested_date format. Use YYYY-MM-DD."}
                )
            context["requested_date"] = requested_date_parsed
        return context


# --- Tracking page --------------------------------------------------------


def _tracking_queryset(request, with_located_before=True):
    """The drivers this user may track, under the page's filters.

    Same scoping as DriverViewSet: management, billing and payroll see every
    company, everyone else their own. `located_before` keeps the drivers whose
    location is older than that moment or was never set, which is what the
    page's "Needs update" tab means (the frontend sends the start of today in
    the dispatcher's time zone).
    """
    qs = Driver.objects.all()

    params = request.query_params
    for name in ("status", "dispatcher"):
        value = params.get(name, "")
        if value:
            if not value.isdigit():
                raise ValidationError({name: "Must be a number."})
            qs = qs.filter(**{f"{name}_id": int(value)})

    search = params.get("search", "").strip()
    if search:
        qs = qs.filter(
            Q(full_name__icontains=search)
            | Q(unit_number__icontains=search)
            | Q(phone_number__icontains=search)
            | Q(email__icontains=search)
        )

    located_before = params.get("located_before", "") if with_located_before else ""
    if located_before:
        moment = parse_datetime(located_before)
        if moment is None:
            raise ValidationError({"located_before": "Must be an ISO 8601 date-time."})
        if timezone.is_naive(moment):
            moment = timezone.make_aware(moment)
        qs = qs.filter(
            Q(current_location_updated_at__lt=moment)
            | Q(current_location_updated_at__isnull=True)
        )
    return qs


class TrackingPagination(CustomPagination):
    page_size = 40
    max_page_size = 200


class TrackingDriverListView(generics.ListAPIView):
    """GET /hiring/tracking/drivers/: the rows of the Tracking page, a page at a time.

    Filters: status, company, dispatcher (ids), search, located_before.
    Ordering: `full_name` (default) or `current_location_updated_at`, which
    puts the drivers who were never located first, then the oldest location.

    Each row says whether the driver has a driver-app account
    (`hub_registered`, None when the hub could not be asked) and shows
    `hub_status` "not_registered" for those who do not: one hub lookup per page.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = TrackingDriverSerializer
    pagination_class = TrackingPagination

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        from hub.registry import apply_registrations

        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = list(page if page is not None else queryset)
        apply_registrations(rows)
        data = self.get_serializer(rows, many=True).data
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    def get_queryset(self):
        qs = (
            _tracking_queryset(self.request)
            .select_related("status", "dispatcher")
            .prefetch_related("vehicles")
        )
        ordering = self.request.query_params.get("ordering", "full_name")
        if ordering == "current_location_updated_at":
            return qs.order_by(
                F("current_location_updated_at").asc(nulls_first=True),
                "full_name",
                "id",
            )
        if ordering == "-current_location_updated_at":
            return qs.order_by(
                F("current_location_updated_at").desc(nulls_last=True),
                "full_name",
                "id",
            )
        return qs.order_by("full_name", "id")


class TrackingSummaryView(views.APIView):
    """GET /hiring/tracking/summary/: the numbers on the Tracking page's tabs and band.

    One request instead of a count per status plus the whole "Needs update"
    list. Takes the list's company, dispatcher and search filters, plus
    `needs_status` (the Active status id) and `located_before` for the needs
    count. Answers {by_status: {id: n}, total, needs_update}.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if request.query_params.get("status"):
            raise ValidationError(
                {
                    "status": "Every status is counted; use needs_status for the needs-update count."
                }
            )
        qs = _tracking_queryset(request, with_located_before=False)
        by_status = {
            str(row["status_id"]): row["n"]
            for row in qs.values("status_id").annotate(n=Count("id")).order_by()
        }
        needs_update = None
        needs_status = request.query_params.get("needs_status", "")
        located_before = request.query_params.get("located_before", "")
        if needs_status and located_before:
            if not needs_status.isdigit():
                raise ValidationError({"needs_status": "Must be a number."})
            moment = parse_datetime(located_before)
            if moment is None:
                raise ValidationError(
                    {"located_before": "Must be an ISO 8601 date-time."}
                )
            if timezone.is_naive(moment):
                moment = timezone.make_aware(moment)
            needs_update = (
                qs.filter(status_id=int(needs_status))
                .filter(
                    Q(current_location_updated_at__lt=moment)
                    | Q(current_location_updated_at__isnull=True)
                )
                .count()
            )
        return Response(
            {
                "by_status": by_status,
                "total": sum(by_status.values()),
                "needs_update": needs_update,
            }
        )
