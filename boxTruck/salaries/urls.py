from django.urls import path

from .views import BonusRuleView, EmployeeDetailView, MonthReportView

urlpatterns = [
    path('report/', MonthReportView.as_view(), name='salaries-report'),
    path('employees/<int:pk>/', EmployeeDetailView.as_view(), name='salaries-employee'),
    path('bonus-rules/', BonusRuleView.as_view(), name='salaries-bonus-rules'),
]
