import random
from datetime import timedelta
from django.db.models import Sum, Count
from config import settings
from django.utils import timezone
from rest_framework import generics, viewsets, views
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from hiring.serializers import DriverViewSerializer
from mobile.auth import DriverTokenAuthentication
from mobile.utils import month_chart, week_chart, year_chart
from users.models import Company
from billing.models import Load
from .models import Driver, DriverLocation, DriverOTP, DriverAuthToken
from .notifications import send_sms, send_email
from .serializers import CompanyViewSerializer, DriverLoadSerializer, DriverLocationViewSerializer, DriverLocationWriteSerializer


class CheckPhoneView(views.APIView):
    permission_classes = []

    def post(self, request):
        phone_number = request.data.get('phone_number')
        if not phone_number:
            return Response({'detail': 'phone_number is required.'}, status=400)

        drivers = Driver.objects.filter(phone_number=phone_number).select_related('company')
        if not drivers.exists():
            all_companies = Company.objects.all()
            return Response({
                'exists': False,
                'has_sms': False,
                'has_email': False,
                'companies': CompanyViewSerializer(all_companies, many=True).data,
            })
        driver = drivers.first()
        return Response({
            'exists': True,
            'has_sms': bool(driver.phone_number),
            'has_email': bool(driver.email),
        })


class SendOTPView(views.APIView):
    permission_classes = []

    def post(self, request):
        phone_number = request.data.get('phone_number')
        method = request.data.get('method')
        if not phone_number or not method:
            return Response({'detail': 'phone_number and method are required.'}, status=400)

        if method not in ['sms', 'email']:
            return Response({'detail': 'method must be sms or email.'}, status=400)

        drivers = Driver.objects.filter(phone_number=phone_number)
        if not drivers.exists():
            return Response({'detail': 'Driver not found.'}, status=404)

        driver = drivers.first()
        if phone_number == settings.TEST_PHONE_NUMBER:
            DriverOTP.objects.filter(
                driver__in=drivers,
                is_used=False,
            ).update(is_used=True)
            DriverOTP.objects.create(
                driver=driver,
                code=settings.TEST_OTP_CODE,
                method=method,
                expires_at=timezone.now() + timedelta(minutes=30),
            )
            return Response({
                'detail': f'Test OTP created successfully. Use code: {settings.TEST_OTP_CODE}'
            })
        if method == 'email' and not driver.email:
            return Response({'detail': 'Driver has no email on file.'}, status=400)

        DriverOTP.objects.filter(
            driver__in=drivers,
            is_used=False,
        ).update(is_used=True)
        code = str(random.randint(100000, 999999))
        DriverOTP.objects.create(
            driver=driver,
            code=code,
            method=method,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        if method == 'sms':
            send_sms(driver.phone_number, f"Your verification code is: {code}. Valid for 5 minutes.")
        else:
            send_email(
                driver.email,
                subject="Your verification code",
                message=f"Your verification code is: {code}. Valid for 5 minutes.",
            )
        return Response({'detail': f'OTP sent via {method}.'})


class VerifyOTPView(views.APIView):
    permission_classes = []

    def post(self, request):
        phone_number = request.data.get('phone_number')
        code = request.data.get('code')
        if not phone_number or not code:
            return Response({'detail': 'phone_number and code are required.'}, status=400)

        assigned_drivers = Driver.objects.filter(
            phone_number=phone_number
        ).select_related('company')
        if not assigned_drivers.exists():
            return Response({'detail': 'Driver not found.'}, status=404)

        otp = DriverOTP.objects.filter(
            driver__in=assigned_drivers,
            code=code,
            is_used=False,
        ).last()
        if not otp or not otp.is_valid():
            return Response({'detail': 'Invalid or expired code.'}, status=400)
        DriverOTP.objects.filter(
            driver__in=assigned_drivers,
            is_used=False,
        ).update(is_used=True)
        return Response({
            'verified': True,
            'phone_number': phone_number,
            'is_registered': True,
            'companies': [
                {
                    'driver_id': d.id,
                    'company': CompanyViewSerializer(d.company).data,
                }
                for d in assigned_drivers
            ]
        })
    

class SelectCompanyView(views.APIView):
    permission_classes = []

    def post(self, request):
        phone_number = request.data.get('phone_number')
        driver_id = request.data.get('driver_id')
        if not phone_number or not driver_id:
            return Response({'detail': 'phone_number and driver_id are required.'}, status=400)

        try:
            driver = Driver.objects.select_related('company').get(
                id=driver_id,
                phone_number=phone_number,
            )
        except Driver.DoesNotExist:
            return Response({'detail': 'Invalid selection.'}, status=400)

        DriverAuthToken.objects.filter(driver=driver, is_active=True).update(is_active=False)
        auth_token = DriverAuthToken.objects.create(
            driver=driver,
            expires_at=timezone.now() + timedelta(days=30),
        )
        return Response({
            'token': str(auth_token.token),
            'driver_id': driver.id,
            'full_name': driver.full_name,
            'company': CompanyViewSerializer(driver.company).data,
            'expires_at': auth_token.expires_at,
        })


class VerifyDriverTokenView(views.APIView):
    permission_classes = []

    def post(self, request):
        token = request.data.get('token')
        if not token:
            return Response({'detail': 'token is required.'}, status=400)

        try:
            auth_token = DriverAuthToken.objects.select_related('driver').get(
                token=token,
                is_active=True,
            )
        except DriverAuthToken.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=401)

        if not auth_token.is_valid():
            return Response({'detail': 'Token expired.'}, status=401)

        return Response({
            'driver_id': auth_token.driver.id,
            'company_id': auth_token.driver.company.id,
            'full_name': auth_token.driver.full_name,
        })


class DriverProfileView(views.APIView):
    authentication_classes = [DriverTokenAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        driver = request.user
        serializer = DriverViewSerializer(driver)
        return Response(serializer.data)
    

class DriverTokenRefreshView(views.APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        token = request.data.get('token')
        if not token:
            return Response({'detail': 'token is required.'}, status=400)

        try:
            auth_token = DriverAuthToken.objects.select_related('driver').get(
                token=token,
            )
        except DriverAuthToken.DoesNotExist:
            return Response({'detail': 'Invalid token.'}, status=401)

        driver = auth_token.driver
        DriverAuthToken.objects.filter(driver=driver, is_active=True).update(is_active=False)
        new_token = DriverAuthToken.objects.create(
            driver=driver,
            expires_at=timezone.now() + timedelta(days=30),
        )
        return Response({
            'token': str(new_token.token),
            'expires_at': new_token.expires_at,
        })


class DriverLocationViewSet(viewsets.ModelViewSet):
    authentication_classes = [DriverTokenAuthentication]
    permission_classes = [AllowAny]
    serializer_class = DriverLocationWriteSerializer
    queryset = DriverLocation.objects.all()


class DriverActiveLoad(views.APIView):
    authentication_classes = [DriverTokenAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        driver = request.user
        history = request.query_params.get('history', 'false').lower() == 'true'
        active_statuses = ['In Transit', 'Dispatched']
        if history:
            loads = Load.objects.filter(
                driver=driver,
            ).exclude(
                status__name__in=active_statuses
            ).select_related(
                'status', 'broker'
            ).prefetch_related(
                'loadstop_set'
            ).order_by('-created_at')
        else:
            loads = Load.objects.filter(
                driver=driver,
                status__name__in=active_statuses,
            ).select_related(
                'status', 'broker'
            ).prefetch_related(
                'loadstop_set'
            ).order_by('-created_at')

        serializer = DriverLoadSerializer(loads, many=True)
        return Response(serializer.data)


class DriverStatisticsView(views.APIView):
    authentication_classes = [DriverTokenAuthentication]
    permission_classes = [AllowAny]

    def get(self, request):
        EXCLUDED_STATUSES = ['Rejected', 'Cancelled']
        driver = request.user
        week = request.query_params.get('week') == 'true'
        month = request.query_params.get('month') == 'true'
        year = request.query_params.get('year') == 'true'
        if not any([week, month, year]):
            week = True

        now = timezone.now()
        if week:
            start_date = now - timedelta(days=now.weekday())  # Monday
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            end_date = start_date + timedelta(days=6, hours=23, minutes=59, seconds=59)
        elif month:
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
            end_date = next_month - timedelta(seconds=1)
        else:
            start_date = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date = now.replace(month=12, day=31, hour=23, minute=59, second=59)

        base_qs = Load.objects.filter(
            driver=driver,
            pickup_date__gte=start_date,
            pickup_date__lte=end_date,
        ).exclude(status__name__in=EXCLUDED_STATUSES)
        aggregates = base_qs.aggregate(
            total_earnings=Sum('driver_pay'),
            loads_completed=Count('id'),
            loaded_miles_sum=Sum('loaded_miles'),
            empty_miles_sum=Sum('empty_miles'),
        )
        total_earnings = float(aggregates['total_earnings'] or 0)
        loads_completed = aggregates['loads_completed'] or 0
        loaded_miles = float(aggregates['loaded_miles_sum'] or 0)
        empty_miles = float(aggregates['empty_miles_sum'] or 0)
        miles_driven = loaded_miles + empty_miles
        per_mile = round(total_earnings / miles_driven, 2) if miles_driven > 0 else 0
        if week:
            chart_data = week_chart(base_qs, start_date)
        elif month:
            chart_data = month_chart(base_qs, start_date)
        else:
            chart_data = year_chart(base_qs, start_date)

        return Response({
            "total_earnings": round(total_earnings, 2),
            "loads_completed": loads_completed,
            "miles_driven": round(miles_driven, 2),
            "per_mile": per_mile,
            "chart": chart_data,
        })
