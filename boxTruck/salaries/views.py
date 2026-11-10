"""Management only: the month's pay per employee, saving salaries, one employee's detail and
the daily bonus rule. Everyone signed in: the month's profit leaderboard."""
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, views
from rest_framework.response import Response

from users.models import CustomUser
from users.permissions import IsAdminUser

from . import services
from .models import BonusRule
from .serializers import BonusRuleSerializer, SaveSalariesSerializer


def _month_or_400(request):
    try:
        return services.parse_month(request.query_params.get('month')), None
    except ValueError:
        return None, Response({'detail': 'month must look like YYYY-MM.'}, status=status.HTTP_400_BAD_REQUEST)


class MonthReportView(views.APIView):
    """GET ?month=YYYY-MM: every employee's pay for the month.
    PUT ?month=YYYY-MM {items: [{user, fixed_salary, profit_percentage}]}: save salaries for the
    month (both null removes the month's row) and return the fresh report."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        first, err = _month_or_400(request)
        if err:
            return err
        return Response(services.month_report(first))

    def put(self, request):
        first, err = _month_or_400(request)
        if err:
            return err
        ser = SaveSalariesSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        services.save_salaries(first, ser.validated_data['items'], request.user)
        return Response(services.month_report(first))


class EmployeeDetailView(views.APIView):
    """GET ?month=YYYY-MM: one employee's loads, bonus days and pay history."""
    permission_classes = [IsAdminUser]

    def get(self, request, pk):
        first, err = _month_or_400(request)
        if err:
            return err
        user = get_object_or_404(CustomUser.objects.select_related('department'), pk=pk)
        return Response(services.employee_detail(user, first))


class BonusRuleView(views.APIView):
    """GET: every rule, newest first. POST {effective_from, profit_step, bonus_amount}: add a rule
    from that date, replacing one already set for the same date."""
    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(BonusRuleSerializer(BonusRule.objects.select_related('created_by'), many=True).data)

    def post(self, request):
        ser = BonusRuleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        rule, _ = BonusRule.objects.update_or_create(
            effective_from=ser.validated_data['effective_from'],
            defaults={'profit_step': ser.validated_data['profit_step'], 'bonus_amount': ser.validated_data['bonus_amount'], 'created_by': request.user},
        )
        return Response(BonusRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


class LeaderboardView(views.APIView):
    """GET ?month=YYYY-MM: profit booked per dispatcher in the month, highest first. Open to every
    signed-in user; it carries no salary, commission or bonus."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        first, err = _month_or_400(request)
        if err:
            return err
        return Response(services.leaderboard(first))
