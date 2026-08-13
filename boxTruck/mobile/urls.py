from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import (
    CheckPhoneView,
    SendOTPView,
    VerifyOTPView,
    VerifyDriverTokenView,
    DriverProfileView,
    DriverTokenRefreshView,
    SelectCompanyView,
    DriverLocationViewSet,
    DriverActiveLoad,
    DriverStatisticsView
)

router = SimpleRouter()
router.register(r'driver-locations', DriverLocationViewSet, basename='driver-locations')

urlpattern = [
    path('auth/check-phone/', CheckPhoneView.as_view(), name='check-phone'),
    path('auth/send-otp/', SendOTPView.as_view(), name='send-otp'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='verify-otp'),
    path('auth/verify-driver-token/', VerifyDriverTokenView.as_view(), name='verify-driver-token'),
    path('auth/select-company/', SelectCompanyView.as_view(), name='select-company'),
    path('driver/profile/', DriverProfileView.as_view(), name='driver-profile'),
    path('driver/token-refresh/', DriverTokenRefreshView.as_view(), name='driver-token-refresh'),
    path('driver/loads/', DriverActiveLoad.as_view(), name='driver-active-load'),
    path('driver/statistics/', DriverStatisticsView.as_view(), name='driver-statistics'),
]

urlpatterns = router.urls + urlpattern