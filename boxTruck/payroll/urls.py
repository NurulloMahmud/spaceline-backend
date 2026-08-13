from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import (ActiveDriversList, InactiveDriversList, StatementViewSet, StatementLoadsViewSet, StatementStatusViewSet,
                    DeductionTypeViewSet, DeductionHistoryByIDAPIView, DeductionViewSet, StatementDeductionViewSet, StatementDeductionsDropDownView,
                    DeductionStatisticsView, CreateDeductionWithStatementIDView
                    )

router = SimpleRouter()
router.register(r'statements', StatementViewSet, basename='statements') #checked
router.register(r'statement-loads', StatementLoadsViewSet, basename='statement-loads')#checked
router.register(r'statement-statuses', StatementStatusViewSet, basename='statement-statuses') #checked
router.register(r'deduction-types', DeductionTypeViewSet, basename='deduction-types') #checked
router.register(r'deductions', DeductionViewSet, basename='deductions') #checked
router.register(r'statement-deductions', StatementDeductionViewSet, basename='statement-deductions')

urlpatterns = [
    path('active-drivers/', ActiveDriversList.as_view(), name='active-drivers'), #checked
    path('inactive-drivers/', InactiveDriversList.as_view(), name='inactive-drivers'), #checked
    path('deductions/<int:pk>/history/', DeductionHistoryByIDAPIView.as_view(), name='deduction-history-by-deduction-id'), #checked
    path('statement-deduction-dropdown/', StatementDeductionsDropDownView.as_view(), name='statement-deductions-dropdown'), #checked
    path('deduction-stats/', DeductionStatisticsView.as_view(), name='deduction-statistics'), #checked
    path('create-deduction/', CreateDeductionWithStatementIDView.as_view(), name='create-company-statement-deduction'),
]

urlpatterns += router.urls
