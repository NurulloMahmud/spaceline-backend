from datetime import timedelta
from django.utils import timezone
import copy
from django.db import transaction
from django.core.files.base import ContentFile
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import ValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework import status, viewsets, views, generics, parsers
from django.db.models import Q
from hiring.google_maps import geocode_zip
from hiring.haversine import haversine
from mobile.models import DriverLocation
from mobile.serializers import DriverLocationViewSerializer
from users.pagination import CustomPagination
from users.permissions import IsAdminUser, IsDispatch, IsDispatchManager, IsInternalService, IsUpdater
from .utils import build_change_description, DEPOSIT_FK_DISPLAY, DRIVER_COMPANY_FK_DISPLAY, DRIVER_FK_DISPLAY, VEHICLE_FK_DISPLAY
from .pdf_generation import fill_w9, generate_contract
from users.models import CustomUser, Team, Company
from .models import (CompanyFile, Deposit, DepositHistory, Driver, DriverCompany, DriverFile, DriverHistory, DriverInviteLink,
                     DriverStatus, Vehicle, VehicleEquipment, VehicleFile,
                     CompanyHistory, VehicleHistory
                     )
from .serializers import (DriverAssignSerializer, UnassignedDriverSerializer, CompanyFileSerializer, DepositHistoryViewSerializer, DepositHistoryWriteSerializer, DepositViewSerializer, DepositWriteSerializer,
                          DriverBulkCreateSerializer, DriverCompanyModalSerializer, DriverFileSerializer, DriverHistoryViewSerializer, DriverHistoryWriteSerializer, DriverListSerializer, DriverViewSerializer,
                          DriverWriteSerializer, DriverStatusSerializer, NearbyDriverSerializer, VehicleDropdownSerializer, VehicleFileSerializer, VehicleHistoryViewSerializer, VehicleHistoryWriteSerializer,
                          VehicleViewSerializer, VehicleWriteSerializer,
                          DriverCompanyViewSerializer, DriverCompanyWriteSerializer,
                          VehicleEquipmentSerializer, CompanyHistoryWriteSerializer, CompanyHistoryViewSerializer)


class DriverStatusViewSet(viewsets.ModelViewSet):
    queryset = DriverStatus.objects.all().order_by('id')
    serializer_class = DriverStatusSerializer
    permission_classes = [IsAuthenticated]


class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    permission_classes = [IsAuthenticated | IsInternalService]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['status', 'company', 'telegram_group_id', 'team', 'dispatcher']
    search_fields = ['full_name', 'ssn', 'phone_number', 'email', 'driver_id', 'address', 'unit_number']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return DriverWriteSerializer
        else:
            return DriverViewSerializer

    def get_queryset(self):
        if self.request.headers.get('X-Internal-Secret'):
            qs = Driver.objects.all()
            telegram_group_id = self.request.query_params.get('telegram_group_id')
            if telegram_group_id:
                qs = qs.filter(telegram_group_id=telegram_group_id)
            return qs
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            return Driver.objects.all()
        return Driver.objects.filter(company=self.request.user.company)
    
    def perform_create(self, serializer):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            serializer.save()
        else:
            serializer.save(company=self.request.user.company, referral_by=self.request.user)
    
    def partial_update(self, request, *args, **kwargs):
        from .google_maps import geocode_zip
        is_internal = bool(request.headers.get('X-Internal-Secret'))
        driver = self.get_object()
        old_instance = copy.deepcopy(driver)
        old_instance.status = driver.status
        old_instance.company = driver.company
        old_instance.manager = driver.manager
        old_instance.referral_by = driver.referral_by
        serializer = self.get_serializer(driver, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_driver = serializer.save()
        updated_driver.refresh_from_db()
        updated_driver.status
        updated_driver.company
        updated_driver.manager
        updated_driver.referral_by
        if 'current_zip' in request.data and request.data['current_zip']:
            geo = geocode_zip(request.data['current_zip'])
            if geo:
                Driver.objects.filter(pk=updated_driver.pk).update(
                    current_city=geo['city'],
                    current_state=geo['state'],
                    current_address=geo['address'],
                    current_longitude=geo['lng'],
                    current_latitude=geo['lat'],
                )
                updated_driver.refresh_from_db()
        if not is_internal:
            description = build_change_description(
                old_instance,
                updated_driver,
                list(request.data.keys()),
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
        from billing.models import Load
        driver = self.get_object()
        if Load.objects.filter(driver=driver).exists():
            raise ValidationError({"detail": "Cannot delete driver: driver has loads on file."})
        with transaction.atomic():
            driver.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DriverDropdownAPIView(generics.ListAPIView):
    serializer_class = DriverViewSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            return Driver.objects.all()
        return Driver.objects.filter(company=self.request.user.company)


class DriverFileViewSet(viewsets.ModelViewSet):
    serializer_class = DriverFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = DriverFile.objects.select_related('driver')
        driver_id = self.request.query_params.get('driver')
        if driver_id:
            qs = qs.filter(driver_id=driver_id)
        return qs


class CompanyFileViewSet(viewsets.ModelViewSet):
    serializer_class = CompanyFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = CompanyFile.objects.select_related('company')
        company_id = self.request.query_params.get('company')
        if company_id:
            qs = qs.filter(company_id=company_id)
        return qs


class VehicleFileViewSet(viewsets.ModelViewSet):
    serializer_class = VehicleFileSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        qs = VehicleFile.objects.select_related('vehicle')
        vehicle_id = self.request.query_params.get('vehicle')
        if vehicle_id:
            qs = qs.filter(vehicle_id=vehicle_id)
        return qs


class VehicleEquipmentViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination
    queryset = VehicleEquipment.objects.all()
    serializer_class = VehicleEquipmentSerializer


class VehicleViewSet(viewsets.ModelViewSet):
    queryset = Vehicle.objects.all()
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['driver__status']
    search_fields = ['driver__full_name', 'make', 'model', 'vin', 'note']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return VehicleWriteSerializer
        else:
            return VehicleViewSerializer
        
    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll', 'hiring']:
            return Vehicle.objects.all()
        return Vehicle.objects.filter(driver__company=self.request.user.company)
    
    def partial_update(self, request, *args, **kwargs):
        vehicle = self.get_object()
        old_instance = copy.deepcopy(vehicle)
        old_instance.driver = vehicle.driver
        old_instance.second_driver = vehicle.second_driver
        serializer = self.get_serializer(vehicle, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_vehicle = serializer.save()
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
    filterset_fields = ['driver']
    search_fields = ['name', 'mc', 'business_type']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return DriverCompanyWriteSerializer
        else:
            return DriverCompanyViewSerializer
        
    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll', 'hiring']:
            return DriverCompany.objects.all()
        return DriverCompany.objects.filter(driver__company=self.request.user.company)
    
    def partial_update(self, request, *args, **kwargs):
        driver_company = self.get_object()
        old_instance = copy.deepcopy(driver_company)
        old_instance.driver = driver_company.driver
        serializer = self.get_serializer(driver_company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_company = serializer.save()
        updated_company.refresh_from_db()
        updated_company.driver
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


class DepositViewSet(viewsets.ModelViewSet):
    queryset = Deposit.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['driver']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return DepositWriteSerializer
        else:
            return DepositViewSerializer

    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll', 'hiring']:
            return Deposit.objects.all()
        return Deposit.objects.filter(driver__company=self.request.user.company)

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


class DriverListView(generics.ListAPIView):
    serializer_class = DriverListSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    ALLOWED_SORT_FIELDS = {
        'created_at',
        'full_name',
        'hired_date',
        'terminated_date',
        'dob',
        'company',
    }

    def get_queryset(self):
        user = self.request.user
        department = user.department.name.lower()

        if department in ['management', 'billing', 'payroll', 'hiring']:
            qs = Driver.objects.all()
        else:
            qs = Driver.objects.filter(company=user.company)

        status_id = self.request.query_params.get('status')
        search = self.request.query_params.get('search')
        manager_id = self.request.query_params.get('manager')
        if manager_id:
            qs = qs.filter(manager_id=manager_id)
        if status_id:
            qs = qs.filter(status_id=status_id)
        if search:
            qs = qs.filter(full_name__icontains=search)

        sort = self.request.query_params.get('sort', '-created_at')
        sort_field = sort.lstrip('-')
        if sort_field in self.ALLOWED_SORT_FIELDS:
            qs = qs.order_by(sort)
        else:
            qs = qs.order_by('-created_at')
        return qs.select_related(
            'company',
            'status',
            'manager'
        )


class DriverCompanyModalView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self, user):
        department = user.department.name.lower()
        if department in ['management', 'billing', 'payroll', 'hiring']:
            return DriverCompany.objects.all()
        return DriverCompany.objects.filter(driver__company=user.company)

    def get(self, request):
        driver_id = request.query_params.get('driver')
        if not driver_id:
            return Response({'detail': 'driver query param is required.'}, status=400)

        qs = self.get_queryset(request.user)
        company = get_object_or_404(
            qs.select_related(
                'driver__status',
            ).prefetch_related(
                'driver__vehicles',
                'companyfile_set',
            ),
            driver_id=driver_id
        )
        serializer = DriverCompanyModalSerializer(company)
        return Response(serializer.data)
    

class GenerateInviteLinkView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        manager_id = request.data.get('manager')
        manager = None
        if manager_id:
            manager = get_object_or_404(CustomUser, id=manager_id)

        invite = DriverInviteLink.objects.create(
            created_by=request.user,
            company=request.user.company,
            manager=manager,
            expires_at=timezone.now() + timedelta(days=7)
        )
        link = f"https://spaceline.boxtruckmanage.com/driver-form/?token={invite.token}"
        return Response({'link': link}, status=201)


SIGN_FILE_NAMES = ('W-9 (Generated)', 'Contractor Agreement (Generated)')


class GenerateDriverSignLinkView(views.APIView):
    """Staff-facing: mint a one-shot, unauthenticated link for an existing
    driver to review and sign their W-9 and contractor agreement."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        driver_id = request.data.get('driver_id')
        if not driver_id:
            return Response({'detail': 'driver_id is required.'}, status=400)

        driver = get_object_or_404(Driver, id=driver_id)

        invite = DriverInviteLink.objects.create(
            created_by=request.user,
            company=driver.company,
            driver=driver,
            expires_at=timezone.now() + timedelta(days=7),
        )
        link = f"https://spaceline.boxtruckmanage.com/driver-sign/?token={invite.token}"
        return Response({'link': link}, status=201)


class DriverSignInfoView(views.APIView):
    """Public, token-based. Curated driver/vehicle/company info plus the
    W-9 and contract files, for the signing page — deliberately excludes
    SSN and banking (Deposit) fields since this endpoint carries no auth."""
    permission_classes = []

    def get(self, request):
        token = request.query_params.get('token')
        if not token:
            return Response({'detail': 'Token is required.'}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related('driver').get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=400)

        if not invite.is_valid():
            return Response({'detail': 'Link has expired or is inactive.'}, status=400)

        driver = invite.driver
        if not driver:
            return Response({'detail': 'This link is not a document-signing link.'}, status=400)

        vehicle = driver.vehicles.first()
        driver_company = DriverCompany.objects.filter(driver=driver).first()
        sign_files = DriverFile.objects.filter(driver=driver, name__in=SIGN_FILE_NAMES)

        return Response({
            'driver': {
                'id': driver.id,
                'full_name': driver.full_name,
                'phone_number': driver.phone_number,
                'email': driver.email,
                'address': driver.address,
                'unit_number': driver.unit_number,
                'city': driver.city,
                'state': driver.state,
                'zip_code': driver.zip_code,
            },
            'vehicle': {
                'id': vehicle.id,
                'vehicle_type': vehicle.vehicle_type,
                'make': vehicle.make,
                'model': vehicle.model,
                'year': vehicle.year,
            } if vehicle else None,
            'company': {
                'name': driver_company.name,
                'mc': driver_company.mc,
                'address': driver_company.address,
                'city': driver_company.city,
                'state': driver_company.state,
                'zipcode': driver_company.zipcode,
            } if driver_company else None,
            'files': [
                {'id': f.id, 'name': f.name, 'url': f.document.url}
                for f in sign_files
            ],
        })


class DriverBulkCreateHRView(views.APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        # if request.user.department.name.lower() not in ['management', 'payroll', 'billing', 'hiring']:
        #     return Response({'detail': 'Not allowed.'}, status=403)

        data = request.data.copy()
        pending_status = get_object_or_404(DriverStatus, name__iexact='pending')
        data['status'] = pending_status.id
        data['company'] = request.user.company.id
        data['referral_by'] = request.user.id
        serializer = DriverBulkCreateSerializer(data=data)
        if serializer.is_valid():
            driver = serializer.save()
            return Response({'detail': 'Driver created.', 'driver_id': driver.id}, status=201)
        return Response(serializer.errors, status=400)


class DriverBulkCreateInviteView(views.APIView):
    permission_classes = []
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        token = request.query_params.get('token')
        if not token:
            return Response({'detail': 'Token is required.'}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related(
                'created_by', 'company'
            ).get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=400)

        if not invite.is_valid():
            return Response({'detail': 'Link has expired or is inactive.'}, status=400)
        data = request.data.copy()
        pending_status = get_object_or_404(DriverStatus, name__iexact='pending')
        data['status'] = pending_status.id
        data['company'] = invite.company.id
        data['referral_by'] = invite.created_by.id
        if invite.manager_id:
            data['manager'] = invite.manager_id
        serializer = DriverBulkCreateSerializer(data=data)
        if serializer.is_valid():
            driver = serializer.save()
            invite.is_active = False
            invite.save(update_fields=['is_active'])
            return Response({'detail': 'Driver created.', 'driver_id': driver.id}, status=201)
        return Response(serializer.errors, status=400)


class DriverExistsCheckView(views.APIView):
    permission_classes = []

    def post(self, request):
        email = (request.data.get('email') or '').strip()
        phone_number = (request.data.get('phone_number') or '').strip()

        if not email and not phone_number:
            return Response({'detail': 'email or phone_number is required.'}, status=400)

        email_exists = False
        if email:
            email_exists = Driver.objects.filter(email__iexact=email).exists()

        phone_exists = False
        if phone_number:
            phone_exists = Driver.objects.filter(phone_number=phone_number).exists()

        return Response({'email_exists': email_exists, 'phone_exists': phone_exists}, status=200)


class DriverInviteSubmitView(views.APIView):
    """Public, token-based endpoint for the driver invite registration form.

    Unlike DriverBulkCreateInviteView (multipart, files included), the frontend
    for this flow sends plain JSON values only, with company/driver/vehicle
    fields mixed flat and nested (some duplicated at both levels). Rather than
    fight DRF's nested-serializer validation for that shape, fields are pulled
    defensively with `_pick`. On success this also generates a W-9 and the
    inviting company's contract PDF and returns their URLs.
    """
    permission_classes = []

    def post(self, request):
        token = request.query_params.get('token')
        if not token:
            return Response({'detail': 'Token is required.'}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related('created_by', 'company').get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=400)

        if not invite.is_valid():
            return Response({'detail': 'Link has expired or is inactive.'}, status=400)

        # A link carrying a driver has already done its registration (or was
        # minted as a sign-link for an existing driver). Registering again
        # would create a second driver from one invite.
        if invite.driver_id:
            return Response(
                {'detail': 'This link has already been used to register a driver.'},
                status=409,
            )

        if not invite.company.contract_template_text:
            return Response(
                {'detail': f"Company '{invite.company.name}' has no contract_template_text configured."},
                status=422,
            )

        data = request.data
        driver_data = data.get('driver') or {}
        vehicle_data = data.get('vehicle') or {}

        def pick(*sources_and_keys, default=''):
            for source, key in sources_and_keys:
                value = source.get(key)
                if value not in (None, ''):
                    return value
            return default

        full_name = pick((driver_data, 'driver_full_name'), (data, 'driver_full_name'))
        phone_number = pick((driver_data, 'phone'), (data, 'phone'))
        company_name = data.get('company_name') or ''

        if not full_name or not company_name:
            return Response(
                {'detail': 'driver_full_name and company_name are required.'}, status=400
            )

        if phone_number and Driver.objects.filter(phone_number=phone_number).exists():
            return Response(
                {'detail': 'A driver with this phone number already exists.'}, status=400
            )

        pending_status = get_object_or_404(DriverStatus, name__iexact='pending')

        dock = vehicle_data.get('dock')
        if isinstance(dock, list):
            dock = ', '.join(str(d) for d in dock if d)

        equipment = vehicle_data.get('equipment') or []
        if not isinstance(equipment, list):
            equipment = [equipment]

        with transaction.atomic():
            driver = Driver.objects.create(
                company=invite.company,
                full_name=full_name,
                phone_number=phone_number,
                emergency_phone_number=data.get('company_emergency_phone') or '',
                status=pending_status,
                referral_by=invite.created_by,
                manager=invite.manager,
                tax_exempt=bool(data.get('tax_exempt', False)),
                payee_code=data.get('payee_code') or '',
                fatca_reporting_code=data.get('fatca_reporting_code') or '',
            )
            driver_company = DriverCompany.objects.create(
                driver=driver,
                name=company_name,
                mc=data.get('company_mc') or '',
                employer_id=data.get('company_employer_id') or '',
                phone_number=data.get('company_phone') or '',
                business_as=data.get('company_doing_business') or '',
                business_type=str(data.get('company_type') or ''),
                zipcode=pick((data, 'company_zip'), (data, 'company_zipcode')),
                state=data.get('company_state') or '',
                city=data.get('company_city') or '',
                address=data.get('company_address') or '',
                email=data.get('company_email') or None,
                applicant_first_name=data.get('company_applicant_first_name') or '',
                applicant_last_name=data.get('company_applicant_last_name') or '',
            )
            vehicle = Vehicle.objects.create(
                driver=driver,
                make=vehicle_data.get('make') or '',
                model=vehicle_data.get('model') or '',
                length=vehicle_data.get('useful_cargo_length') or None,
                width=vehicle_data.get('useful_cargo_width') or None,
                height=vehicle_data.get('useful_cargo_height') or None,
                gvw=vehicle_data.get('GVW_lbs') or 0,
                payload=vehicle_data.get('payload_lbs') or 0,
                door_open_width=vehicle_data.get('door_width') or None,
                door_open_height=vehicle_data.get('door_height') or None,
                dock_height=dock or '',
            )
            VehicleEquipment.objects.bulk_create([
                VehicleEquipment(vehicle=vehicle, name=str(name))
                for name in equipment if name
            ])

            w9_bytes = fill_w9({
                'company_name': company_name,
                'company_doing_business': data.get('company_doing_business') or '',
                'company_address': data.get('company_address') or '',
                'company_city': data.get('company_city') or '',
                'company_state': data.get('company_state') or '',
                'company_zip': driver_company.zipcode,
                'company_employer_id': data.get('company_employer_id') or '',
                'company_type': data.get('company_type') or '',
                'payee_code': driver.payee_code,
                'fatca_reporting_code': driver.fatca_reporting_code,
            }).getvalue()

            contractor_address = ', '.join(
                part for part in [
                    data.get('company_address'), data.get('company_city'),
                    data.get('company_state'), driver_company.zipcode,
                ] if part
            )
            today = timezone.now()
            contract_bytes = generate_contract(invite.company, {
                'effective_day': today.strftime('%d'),
                'effective_month': today.strftime('%B'),
                'effective_year': today.strftime('%y'),
                'contractor_name': company_name,
                'contractor_address': contractor_address,
                'contractor_email': data.get('company_email') or '',
            }).getvalue()
            w9_file = DriverFile(driver=driver, name='W-9 (Generated)')
            w9_file.document.save(f'w9_{driver.id}.pdf', ContentFile(w9_bytes), save=True)
            contract_file = DriverFile(driver=driver, name='Contractor Agreement (Generated)')
            contract_file.document.save(f'contract_{driver.id}.pdf', ContentFile(contract_bytes), save=True)

            # The link deliberately stays active. Registration is only half the
            # flow: the driver still has to sign the two PDFs generated above
            # and post them back to /driver/invite/documents/, which
            # authenticates with this same token. Deactivating here left every
            # driver unable to return their signed W-9 and contract.
            #
            # Claiming the invite with the driver it just created is what stops
            # it being re-registered — spent for this step, still usable for the
            # next. The upload endpoint is what finally deactivates it.
            invite.driver = driver
            invite.save(update_fields=['driver'])

        return Response({
            'detail': 'Driver created.',
            'driver_id': driver.id,
            'w9_url': w9_file.document.url,
            'contract_url': contract_file.document.url,
        }, status=201)


class DriverInviteDocumentUploadView(views.APIView):
    permission_classes = []
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        token = request.query_params.get('token')
        if not token:
            return Response({'detail': 'Token is required.'}, status=400)

        try:
            invite = DriverInviteLink.objects.select_related('driver').get(token=token)
        except DriverInviteLink.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=400)

        if not invite.is_valid():
            return Response({'detail': 'Link has expired or is inactive.'}, status=400)

        # The driver comes from the invite, not the request body. This endpoint
        # is unauthenticated, so a token holder who could name any driver_id
        # could attach files to any driver in the company.
        driver = invite.driver
        if driver is None:
            return Response(
                {'detail': 'Submit the registration form before uploading documents.'},
                status=409,
            )

        # driver_id is still accepted for the documented request shape, but it
        # only ever confirms what the token already decided.
        requested_id = request.data.get('driver_id')
        if requested_id not in (None, '') and str(requested_id) != str(driver.id):
            return Response(
                {'detail': 'driver_id does not match this link.'}, status=400
            )

        files = request.FILES.getlist('files')
        names = request.data.getlist('names') if hasattr(request.data, 'getlist') else request.data.get('names', [])
        if len(files) != len(names):
            return Response({'detail': 'files and names must have the same count.'}, status=400)

        created = DriverFile.objects.bulk_create([
            DriverFile(driver=driver, name=name, document=file)
            for file, name in zip(files, names)
        ])

        invite.is_active = False
        invite.save(update_fields=['is_active'])

        return Response({
            'detail': 'Documents uploaded.',
            'files': [{'id': f.id, 'name': f.name, 'url': f.document.url} for f in created],
        }, status=201)


class CompanyHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['company']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return CompanyHistoryWriteSerializer
        else:
            return CompanyHistoryViewSerializer
        
    def get_queryset(self):
        return CompanyHistory.objects.select_related(
            'changed_by', 'company'
        ).all()


class DriverHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['driver']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return DriverHistoryWriteSerializer
        else:
            return DriverHistoryViewSerializer
        
    def get_queryset(self):
        return DriverHistory.objects.select_related(
            'changed_by', 'driver'
        ).all()
        

class VehicleHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['vehicle']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return VehicleHistoryWriteSerializer
        else:
            return VehicleHistoryViewSerializer
        
    def get_queryset(self):
        return VehicleHistory.objects.select_related(
            'changed_by', 'vehicle'
        ).all()


class DepositHistoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ['deposit']

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PATCH']:
            return DepositHistoryWriteSerializer
        else:
            return DepositHistoryViewSerializer

    def get_queryset(self):
        return DepositHistory.objects.select_related(
            'changed_by', 'deposit'
        ).all()


class DriverBulkView(views.APIView):
    permission_classes = [IsInternalService]

    def get(self, request):
        ids = request.query_params.get('ids', '')
        if not ids:
            return Response([])
        id_list = [i for i in ids.split(',') if i.strip().isdigit()]
        drivers = Driver.objects.filter(id__in=id_list).values(
            'id', 'full_name'
        )
        return Response(list(drivers))


class DriverLocationByIDAPIView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = DriverLocationViewSerializer

    def get_queryset(self):
        driver = self.kwargs.get('pk')
        return DriverLocation.objects.filter(driver=driver)


class DriverNearbyView(views.APIView):
    permission_classes = [IsAuthenticated | IsInternalService]

    def get(self, request):
        zip_code = request.query_params.get('zip')
        radius = float(request.query_params.get('radius', 100))
        if not zip_code:
            return Response({'detail': 'zip is required.'}, status=400)

        geo = geocode_zip(zip_code)
        if not geo:
            return Response({'detail': 'Could not geocode the provided zip code.'}, status=400)

        pickup_lat = geo['lat']
        pickup_lng = geo['lng']

        is_internal = bool(request.headers.get('X-Internal-Secret'))
        qs = Driver.objects.filter(
            status__name__iexact='active',
            current_latitude__isnull=False,
            current_longitude__isnull=False,
        ).select_related(
            'status', 'company'
        ).prefetch_related(
            'vehicles',
            'vehicles__vehicleequipment_set',
        )

        if not is_internal:
            department = request.user.department.name.lower() if request.user.department else ''
            if department != 'management':
                qs = qs.filter(company=request.user.company)

        nearby = []
        for driver in qs:
            miles = haversine(pickup_lat, pickup_lng, driver.current_latitude, driver.current_longitude)
            if miles <= radius:
                driver._miles_out = miles
                nearby.append(driver)

        nearby.sort(key=lambda d: d._miles_out)
        serializer = NearbyDriverSerializer(nearby, many=True)
        return Response({
            'pickup': {
                'zip': zip_code,
                'lat': pickup_lat,
                'lng': pickup_lng,
                'city': geo['city'],
                'state': geo['state'],
            },
            'radius_miles': radius,
            'count': len(nearby),
            'drivers': serializer.data,
        })
    

class VehicleDropdownView(generics.ListAPIView):
    serializer_class = VehicleDropdownSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            return Vehicle.objects.all()
        return Vehicle.objects.filter(driver__company=self.request.user.company)


class DriverAssignmentView(views.APIView):
    """
    Assign drivers to a dispatch team and/or a dispatcher, in bulk.

    Send `team` or `dispatcher` as null to clear that field. Drivers outside
    the caller's company are skipped rather than silently reassigned, and the
    response names them so the UI can say what did not happen.
    """
    permission_classes = [IsAdminUser | IsDispatchManager | IsDispatch | IsUpdater]

    def post(self, request):
        serializer = DriverAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        driver_ids = data['driver_ids']

        department = getattr(request.user, 'department', None)
        is_management = bool(department and department.name.lower() in
                             ['management', 'billing', 'payroll'])

        drivers = Driver.objects.filter(id__in=driver_ids)
        if not is_management:
            drivers = drivers.filter(company=request.user.company)

        found = set(drivers.values_list('id', flat=True))
        rejected = [d for d in driver_ids if d not in found]

        updates = {}

        if 'team' in data:
            team_id = data['team']
            if team_id is None:
                updates['team'] = None
            else:
                team = Team.objects.filter(id=team_id).first()
                if not team:
                    return Response({"team": "Team not found."},
                                    status=status.HTTP_400_BAD_REQUEST)
                if not is_management and team.company_id != request.user.company_id:
                    return Response({"team": "That team belongs to another company."},
                                    status=status.HTTP_403_FORBIDDEN)
                updates['team'] = team

        if 'dispatcher' in data:
            dispatcher_id = data['dispatcher']
            if dispatcher_id is None:
                updates['dispatcher'] = None
            else:
                dispatcher = CustomUser.objects.filter(id=dispatcher_id).first()
                if not dispatcher:
                    return Response({"dispatcher": "User not found."},
                                    status=status.HTTP_400_BAD_REQUEST)
                if not is_management and dispatcher.company_id != request.user.company_id:
                    return Response({"dispatcher": "That user belongs to another company."},
                                    status=status.HTTP_403_FORBIDDEN)
                updates['dispatcher'] = dispatcher

        updated = drivers.update(**updates) if updates else 0

        return Response({
            "updated": updated,
            "rejected_driver_ids": rejected,
            "detail": ("Drivers outside your company were skipped."
                       if rejected else "All drivers updated."),
        })


class UnassignedDriversAPIView(generics.ListAPIView):
    """
    Drivers missing a team or a dispatcher — the worklist for finishing
    assignment. `?missing=team|dispatcher|either` (default either).
    """
    serializer_class = UnassignedDriverSerializer
    permission_classes = [IsAdminUser | IsDispatchManager | IsDispatch | IsUpdater]
    pagination_class = CustomPagination

    def get_queryset(self):
        queryset = Driver.objects.all()
        department = getattr(self.request.user, 'department', None)
        if not (department and department.name.lower() in ['management', 'billing', 'payroll']):
            queryset = queryset.filter(company=self.request.user.company)

        missing = self.request.query_params.get('missing', 'either')
        if missing == 'team':
            queryset = queryset.filter(team__isnull=True)
        elif missing == 'dispatcher':
            queryset = queryset.filter(dispatcher__isnull=True)
        else:
            queryset = queryset.filter(
                Q(team__isnull=True) | Q(dispatcher__isnull=True)
            )
        return queryset.select_related('company', 'status', 'team', 'dispatcher').order_by('full_name')
