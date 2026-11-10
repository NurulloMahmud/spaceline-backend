from django.urls import path

from .views import BonusRuleView, EmployeeDetailView, LeaderboardLoadsView, LeaderboardView, MonthReportView

urlpatterns = [
    path('report/', MonthReportView.as_view(), name='salaries-report'),
    path('employees/<int:pk>/', EmployeeDetailView.as_view(), name='salaries-employee'),
    path('leaderboard/', LeaderboardView.as_view(), name='salaries-leaderboard'),
    path('leaderboard/<int:pk>/loads/', LeaderboardLoadsView.as_view(), name='salaries-leaderboard-loads'),
    path('bonus-rules/', BonusRuleView.as_view(), name='salaries-bonus-rules'),
]
