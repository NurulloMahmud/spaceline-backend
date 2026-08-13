from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .views import DispatchBonusViewSet, DispatchSalaryViewSet, DispatcherSalaryView, DispatcherOwnSalaryView, DispatcherTargetView

router = SimpleRouter()
router.register(r'bonuses', DispatchBonusViewSet, basename='bonus')
router.register(r'salary-rates', DispatchSalaryViewSet, basename='salary')

urlpattern = [
    path('dispatcher-salary/', DispatcherSalaryView.as_view(), name='dispatcher-salary'),
    path('dispatcher-own-salary/', DispatcherOwnSalaryView.as_view(), name='dispatcher-own-salary'),
    path('target/', DispatcherTargetView.as_view(), name='dispatcher-target'),
]

urlpatterns = router.urls + urlpattern
