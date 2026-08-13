from calendar import monthrange
from datetime import date, datetime, time
from decimal import Decimal
from django.utils import timezone
from rest_framework import viewsets, views, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from billing.models import Load
from dispatchers.utils import calculate_bonus, calculate_dispatcher_kpi, calculate_target, get_company_actual_gross_by_dispatcher, get_company_dispatcher_targets, get_dispatcher_gross, get_previous_3_months
from users.models import Company, CustomUser
from users.permissions import IsAdminUser
from .serializers import DispatchSalaryViewSerializer, DispatchBonusViewSerializer, DispatchSalaryWriteSerializer, DispatchBonusWriteSerializer
from .models import DispatchSalary, DispatchBonus


class DispatchSalaryViewSet(viewsets.ModelViewSet):

    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            return DispatchSalary.objects.all().order_by('company__name')
        return DispatchSalary.objects.filter(company=self.request.user.company).order_by('company__name')

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return DispatchSalaryWriteSerializer
        return DispatchSalaryViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]

    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user,
            updated_by=self.request.user
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
    

class DispatchBonusViewSet(viewsets.ModelViewSet):
    
    def get_queryset(self):
        if self.request.user.department.name.lower() in ['management', 'billing', 'payroll']:
            return DispatchBonus.objects.all()
        return DispatchBonus.objects.filter(company=self.request.user.company)

    def get_serializer_class(self):
        if self.request.method in ['POST', 'PUT', 'PATCH']:
            return DispatchBonusWriteSerializer
        return DispatchBonusViewSerializer

    def get_permissions(self):
        if self.request.method == 'GET':
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAdminUser]
        return [permission() for permission in permission_classes]
    
    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user,
            updated_by=self.request.user
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
    

class DispatcherSalaryView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        EARLIEST_DATE = date(2026, 6, 1)
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        if not start_date_str or not end_date_str:
            return Response({'detail': 'start_date and end_date are required.'}, status=400)

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)

        if start_date < EARLIEST_DATE:
            return Response({'detail': 'You cannot view data before June 2026.'}, status=400)

        user = request.user
        department = user.department.name.lower() if user.department else ''
        is_management = department == 'management'
        if is_management:
            companies = Company.objects.all()
        else:
            companies = Company.objects.filter(id=user.company.id)

        current_month_start = start_date.replace(day=1)
        local_tz = timezone.get_current_timezone()
        month_start_dt = timezone.make_aware(datetime.combine(current_month_start, time.min), local_tz)
        month_last_day = monthrange(current_month_start.year, current_month_start.month)[1]
        month_end_dt = timezone.make_aware(datetime.combine(current_month_start.replace(day=month_last_day), time.max), local_tz)
        now_dt = min(timezone.now(), month_end_dt)
        result = []
        for company in companies:
            dispatchers = CustomUser.objects.filter(
                company=company,
                department__name__iexact='dispatch',
            ).select_related('department', 'company')
            if not dispatchers.exists():
                continue
            company_targets = get_company_dispatcher_targets(
                company, current_month_start, dispatcher_ids=[d.id for d in dispatchers]
            )
            company_actual_gross = get_company_actual_gross_by_dispatcher(company, month_start_dt, now_dt)
            dispatchers_result = []
            for dispatcher in dispatchers:
                loads = Load.objects.filter(
                    company=company,
                    booked_by=dispatcher,
                    pickup_date__date__gte=start_date,
                    pickup_date__date__lte=end_date,
                ).exclude(
                    status__name__iexact='rejected'
                ).select_related(
                    'driver', 'status'
                )

                kpi = calculate_dispatcher_kpi(
                    dispatcher=dispatcher,
                    loads=list(loads),
                    company=company,
                    start_date=start_date,
                    end_date=end_date,
                )
                target_info = company_targets.get(dispatcher.id, {"target": 0, "based_on": []})
                actual_gross = float(company_actual_gross.get(dispatcher.id, Decimal('0')))
                kpi['monthly_target'] = {
                    'current_month': current_month_start.strftime('%B %Y'),
                    'target': target_info['target'],
                    'based_on': target_info['based_on'],
                    'actual_gross': actual_gross,
                    'remaining_to_target': max(target_info['target'] - actual_gross, 0),
                    'is_on_track': actual_gross >= target_info['target'],
                    'bonus_earned': calculate_bonus(actual_gross, target_info['target']),
                }

                if kpi['grand_total'] == 0 and actual_gross == 0 and not kpi['loads']:
                    continue

                dispatchers_result.append(kpi)
            if not dispatchers_result:
                continue
            result.append({
                'company': {'id': company.id, 'name': company.name},
                'dispatchers': dispatchers_result,
            })
        return Response(result)


class DispatcherOwnSalaryView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        EARLIEST_DATE = date(2026, 6, 1)
        user = request.user
        if not user.department or user.department.name.lower() != 'dispatch':
            return Response({'detail': 'Only dispatchers can access this endpoint.'}, status=403)
        
        today = date.today()
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        if start_date_str and end_date_str:
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except ValueError:
                return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)
        else:
            start_date = today.replace(day=1)
            last_day = monthrange(today.year, today.month)[1]
            end_date = today.replace(day=last_day)
        if start_date < EARLIEST_DATE:
            return Response({'detail': 'You cannot view data before June 2026.'}, status=400)
        loads = Load.objects.filter(
            company=user.company,
            booked_by=user,
            pickup_date__date__gte=start_date,
            pickup_date__date__lte=end_date,
        ).exclude(
            status__name__iexact='rejected'
        ).select_related(
            'driver', 'status'
        )
        kpi = calculate_dispatcher_kpi(
            dispatcher=user,
            loads=list(loads),
            company=user.company,
            start_date=start_date,
            end_date=end_date,
        )
        return Response({
            'period': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat(),
            },
            'salary': kpi,
        })


class DispatcherTargetView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.department or user.department.name.lower() != 'dispatch':
            return Response({'detail': 'Only dispatchers can access this endpoint.'}, status=403)

        today = date.today()
        current_month_start = today.replace(day=1)
        months = get_previous_3_months(current_month_start)
        last_three_months = []
        for start_dt, end_dt, label in months:
            gross = get_dispatcher_gross(user, user.company, start_dt, end_dt)
            if gross == 0:
                gross = Decimal('3000')
            last_three_months.append({"month": label, "gross": float(gross)})
        target_data = calculate_target(last_three_months)
        target = target_data['target']
        local_tz = timezone.get_current_timezone()
        month_start_dt = timezone.make_aware(datetime.combine(current_month_start, time.min), local_tz)
        now_dt = timezone.now()
        actual_gross = get_dispatcher_gross(user, user.company, month_start_dt, now_dt)
        bonus = calculate_bonus(float(actual_gross), target)
        return Response({
            'current_month': current_month_start.strftime('%B %Y'),
            'based_on': last_three_months,
            'target': target,
            'actual_gross': float(actual_gross),
            'remaining_to_target': max(target - float(actual_gross), 0),
            'is_on_track': float(actual_gross) >= target,
            'bonus_earned': bonus,
        })
