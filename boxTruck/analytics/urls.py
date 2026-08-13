from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import (LoadAnalyticsAPIView, LoadStatusAnalyticsAPIView, DriverWeeklyPerformanceAPIView, LoadSummaryAnalyticsAPIView
                    )

router = SimpleRouter()

urlpattern = [
    path('load-summary/', LoadAnalyticsAPIView.as_view(), name='load-summary'),
    path('load-status/', LoadStatusAnalyticsAPIView.as_view(), name='load-status-analytics'),
    path('driver-performance/', DriverWeeklyPerformanceAPIView.as_view(), name='driver-weekly-performance'),
    path('load-summary-analytics/', LoadSummaryAnalyticsAPIView.as_view(), name='load-summary-analytics'),
]

urlpatterns = router.urls + urlpattern
