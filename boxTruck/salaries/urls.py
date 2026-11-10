from django.urls import path

from .views import BonusRuleView, EmployeeDetailView, LeaderboardView, MonthReportView

urlpatterns = [
    path('report/', MonthReportView.as_view(), name='salaries-report'),
    path('employees/<int:pk>/', EmployeeDetailView.as_view(), name='salaries-employee'),
    path('leaderboard/', LeaderboardView.as_view(), name='salaries-leaderboard'),
    path('bonus-rules/', BonusRuleView.as_view(), name='salaries-bonus-rules'),
]
