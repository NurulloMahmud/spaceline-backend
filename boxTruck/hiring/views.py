from datetime import timedelta
from django.utils import timezone
import copy
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
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
from .utils import build_change_description, DRIVER_COMPANY_FK_DISPLAY, DRIVER_FK_DISPLAY, VEHICLE_FK_DISPLAY
from users.models import CustomUser, Team
from .models import (CompanyFile, Driver, DriverCompany, DriverFile, DriverHistory, DriverInviteLink, 
                     DriverStatus, Vehicle, VehicleEquipment, VehicleFile,
                     CompanyHistory, VehicleHistory
                     )
from .serializers import (DriverAssignSerializer, UnassignedDriverSerializer, CompanyFileSerializer, DriverBulkCreateSerializer, DriverCompanyModalSerializer, DriverFileSerializer, DriverHistoryViewSerializer, DriverHistoryWriteSerializer, DriverListSerializer, DriverViewSerializer, 
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


class DriverListView(generics.ListAPIView):
    serializer_class = DriverListSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

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
        return qs.select_related('company', 'status', 'manager')


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
        invite = DriverInviteLink.objects.create(
            created_by=request.user,
            company=request.user.company,
            expires_at=timezone.now() + timedelta(days=7)
        )
        link = f"https://boxmanage.smartfleetllc.com/driver-form/?token={invite.token}"
        return Response({'link': link}, status=201)


class DriverBulkCreateHRView(views.APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        if request.user.department.name.lower() not in ['management', 'payroll', 'billing', 'hiring']:
            return Response({'detail': 'Not allowed.'}, status=403)

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
        serializer = DriverBulkCreateSerializer(data=data)
        if serializer.is_valid():
            driver = serializer.save()
            return Response({'detail': 'Driver created.', 'driver_id': driver.id}, status=201)
        return Response(serializer.errors, status=400)


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
