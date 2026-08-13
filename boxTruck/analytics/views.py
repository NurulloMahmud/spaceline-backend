from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from hiring.models import Driver
from datetime import datetime, timedelta
from django.utils.timezone import localdate
from users.permissions import IsAdminUser
from datetime import datetime
import calendar
from django.db.models.functions import TruncMonth
from django.db.models import Sum, Count
from billing.models import Load


class LoadAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        year = int(request.query_params.get('year', datetime.now().year))
        company_id = request.query_params.get('company', None)
        loads = Load.objects.filter(pickup_date__year=year, drop_date__year=year).exclude(
            status__name__in=['Cancelled', 'Rejected']
        ).exclude(
            company__name__iexact='Smart Fleet LLC'
        )
        if company_id:
            loads = loads.filter(company_id=company_id)
        driver_data = loads.annotate(month=TruncMonth('pickup_date')).values('company_id', 'company__name', 'month').annotate(total_driver_pay=Sum('driver_pay'), total_carrier_pay=Sum('carrier_pay')).order_by('company_id', 'month')
        month_name_map = {i: calendar.month_name[i] for i in range(1, 13)}
        response_data = {}
        monthly_totals = {
            "monthly_driver_pay": {name: 0.0 for name in month_name_map.values() if name},
            "monthly_carrier_pay": {name: 0.0 for name in month_name_map.values() if name},
            "monthly_profit": {name: 0.0 for name in month_name_map.values() if name},
        }
        for entry in driver_data:
            company_id = entry['company_id']
            company_name = entry['company__name']
            month_number = entry['month'].month
            month_name = month_name_map[month_number]
            if company_id not in response_data:
                response_data[company_id] = {
                    'company_id': company_id,
                    'company_name': company_name,
                    "monthly_driver_pay": {name: 0.0 for name in month_name_map.values() if name},
                    "monthly_carrier_pay": {name: 0.0 for name in month_name_map.values() if name},
                    "monthly_profit": {name: 0.0 for name in month_name_map.values() if name},
                }
            driver_pay = float(entry['total_driver_pay'] or 0)
            carrier_pay = float(entry['total_carrier_pay'] or 0)
            profit = carrier_pay - driver_pay
            response_data[company_id]['monthly_driver_pay'][month_name] = driver_pay
            response_data[company_id]['monthly_carrier_pay'][month_name] = carrier_pay
            response_data[company_id]['monthly_profit'][month_name] = profit
            monthly_totals["monthly_driver_pay"][month_name] += driver_pay
            monthly_totals["monthly_carrier_pay"][month_name] += carrier_pay
            monthly_totals["monthly_profit"][month_name] += profit
        response_data["total"] = {
            "company_id": None,
            "company_name": "Total",
            "monthly_driver_pay": monthly_totals["monthly_driver_pay"],
            "monthly_carrier_pay": monthly_totals["monthly_carrier_pay"],
            "monthly_profit": monthly_totals["monthly_profit"],
        }
        return Response(list(response_data.values()))


class LoadStatusAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        year = int(request.query_params.get('year', datetime.now().year))
        month = request.query_params.get('month')
        company_id = request.query_params.get('company', None)
        filters = {'pickup_date__year': year}
        if month:
            filters['pickup_date__month'] = int(month)
        if company_id:
            filters['company_id'] = int(company_id)
        loads = Load.objects.filter(**filters).exclude(
        status__name__in=['Cancelled', 'Rejected']
        ).exclude(
            company__name__iexact='Smart Fleet LLC'
        ).values(
            'status__name',
            'company__name'
        ).annotate(
            total_driver_pay=Sum('driver_pay')
        ).order_by('status__name', 'company__name')
        result = {}
        for entry in loads:
            status_name = entry['status__name'] or "Unknown"
            company_name = entry['company__name'] or "Unknown"
            driver_pay = float(entry['total_driver_pay'] or 0)
            if status_name not in result:
                result[status_name] = {
                    'total_driver_pay': 0.0,
                    'info': {}
                }
            result[status_name]['total_driver_pay'] += driver_pay
            result[status_name]['info'][company_name] = (
                result[status_name]['info'].get(company_name, 0.0) + driver_pay
            )
        return Response(result)


class DriverWeeklyPerformanceAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        company_id = request.query_params.get("company")
        start_date_param = request.query_params.get("start_date")
        end_date_param = request.query_params.get("end_date")

        try:
            if start_date_param:
                start_date = datetime.strptime(start_date_param, "%Y-%m-%d").date()
            else:
                start_date = localdate()

            if end_date_param:
                end_date = datetime.strptime(end_date_param, "%Y-%m-%d").date()
            else:
                end_date = start_date + timedelta(days=6)

        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=400
            )
        if end_date < start_date:
            return Response(
                {"detail": "end_date cannot be earlier than start_date"},
                status=400
            )

        active_drivers_qs = Driver.objects.filter(
            status__name__iexact="Active"
        )

        if company_id:
            active_drivers_qs = active_drivers_qs.filter(company_id=company_id)

        active_driver_ids = list(active_drivers_qs.values_list("id", flat=True))
        active_drivers_count = len(active_driver_ids)
        total_days = (end_date - start_date).days + 1
        response_data = []
        for i in range(total_days):
            current_day = start_date + timedelta(days=i)

            drivers_with_load_count = (
                Load.objects.filter(
                    driver_id__in=active_driver_ids,
                    pickup_date__date__lte=current_day,
                    drop_date__date__gte=current_day,
                )
                .exclude(status__name__in=["Cancelled", "Rejected"])
                .values("driver_id")
                .distinct()
                .count()
            )

            drivers_without_load = active_drivers_count - drivers_with_load_count
            utilization_percent = round(
                (drivers_with_load_count / active_drivers_count * 100), 2
            ) if active_drivers_count > 0 else 0.0

            response_data.append({
                "date": current_day.strftime("%Y-%m-%d"),
                "day": current_day.strftime("%A"),
                "active_drivers": active_drivers_count,
                "drivers_with_load": drivers_with_load_count,
                "drivers_without_load": drivers_without_load,
                "utilization_percent": utilization_percent,
            })
        return Response({
            "company_id": company_id,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "driver_performance": response_data
        })


class LoadSummaryAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        company_id = request.query_params.get("company")
        start_date_param = request.query_params.get("start_date")
        end_date_param = request.query_params.get("end_date")

        if not start_date_param or not end_date_param:
            return Response(
                {"detail": "start_date and end_date are required. Use YYYY-MM-DD"},
                status=400
            )

        try:
            start_date = datetime.strptime(start_date_param, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_param, "%Y-%m-%d").date()
        except ValueError:
            return Response(
                {"detail": "Invalid date format. Use YYYY-MM-DD"},
                status=400
            )

        if end_date < start_date:
            return Response(
                {"detail": "end_date cannot be earlier than start_date"},
                status=400
            )

        loads = Load.objects.filter(
            pickup_date__date__range=[start_date, end_date]
        ).exclude(
            status__name__in=["Cancelled", "Rejected"]
        )

        if company_id:
            loads = loads.filter(company_id=company_id)

        aggregated = loads.aggregate(
            loads_count=Count("id"),
            total_carrier_pay=Sum("carrier_pay"),
            total_driver_pay=Sum("driver_pay"),
        )

        total_carrier_pay = aggregated["total_carrier_pay"] or Decimal("0.00")
        total_driver_pay = aggregated["total_driver_pay"] or Decimal("0.00")
        total_profit = total_carrier_pay - total_driver_pay

        return Response({
            "company_id": company_id,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "loads_count": aggregated["loads_count"] or 0,
            "total_carrier_pay": float(total_carrier_pay),
            "total_driver_pay": float(total_driver_pay),
            "total_profit": float(total_profit),
        })
