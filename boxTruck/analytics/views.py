from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from datetime import datetime
from users.permissions import IsAdminUser
from datetime import datetime
import calendar
from django.db.models.functions import TruncMonth
from django.db.models import Sum, Count
from billing.models import Load
from accounting.services import service_fee_profit


class LoadAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        year = int(request.query_params.get('year', datetime.now().year))
        loads = Load.objects.filter(pickup_date__year=year, drop_date__year=year).exclude(
            status__name__in=['Cancelled', 'Rejected']
        )
        driver_data = loads.annotate(month=TruncMonth('pickup_date')).values('month').annotate(total_driver_pay=Sum('driver_pay'), total_carrier_pay=Sum('carrier_pay')).order_by('month')
        month_name_map = {i: calendar.month_name[i] for i in range(1, 13)}
        monthly_totals = {
            "monthly_driver_pay": {name: 0.0 for name in month_name_map.values() if name},
            "monthly_carrier_pay": {name: 0.0 for name in month_name_map.values() if name},
            "monthly_profit": {name: 0.0 for name in month_name_map.values() if name},
        }
        for entry in driver_data:
            month_name = month_name_map[entry['month'].month]
            driver_pay = float(entry['total_driver_pay'] or 0)
            carrier_pay = float(entry['total_carrier_pay'] or 0)
            monthly_totals["monthly_driver_pay"][month_name] += driver_pay
            monthly_totals["monthly_carrier_pay"][month_name] += carrier_pay
            monthly_totals["monthly_profit"][month_name] += carrier_pay - driver_pay
        return Response([{"company_id": None, "company_name": "Total", **monthly_totals}])


class LoadStatusAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        year = int(request.query_params.get('year', datetime.now().year))
        month = request.query_params.get('month')
        filters = {'pickup_date__year': year}
        if month:
            filters['pickup_date__month'] = int(month)
        loads = Load.objects.filter(**filters).exclude(
        status__name__in=['Cancelled', 'Rejected']
        ).values(
            'status__name'
        ).annotate(
            total_driver_pay=Sum('driver_pay')
        ).order_by('status__name')
        result = {}
        for entry in loads:
            status_name = entry['status__name'] or "Unknown"
            driver_pay = float(entry['total_driver_pay'] or 0)
            if status_name not in result:
                result[status_name] = {
                    'total_driver_pay': 0.0,
                }
            result[status_name]['total_driver_pay'] += driver_pay
        return Response(result)


class LoadSummaryAnalyticsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
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

        aggregated = loads.aggregate(
            loads_count=Count("id"),
            total_carrier_pay=Sum("carrier_pay"),
            total_driver_pay=Sum("driver_pay"),
        )

        total_carrier_pay = aggregated["total_carrier_pay"] or Decimal("0.00")
        total_driver_pay = aggregated["total_driver_pay"] or Decimal("0.00")
        total_profit = total_carrier_pay - total_driver_pay
        service_fees = service_fee_profit(start_date, end_date)

        return Response({
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "loads_count": aggregated["loads_count"] or 0,
            "total_carrier_pay": float(total_carrier_pay),
            "total_driver_pay": float(total_driver_pay),
            "total_profit": float(total_profit),
            "service_fee_profit": float(service_fees["quick_pay"] + service_fees["expenses"]),
            "service_fee_quick_pay": float(service_fees["quick_pay"]),
            "service_fee_expenses": float(service_fees["expenses"]),
        })
