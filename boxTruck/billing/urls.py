from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import (BrokersViewSet, LoadStopsViewSet,
                    LoadsViewSet, LoadStatusesViewSet, PaymentTypeViewSet, LoadFilesAPIView, LoadHistoryByIdAPIView, RateConfirmationUploadView,
                    DailyReportByDispatchersAPIView, DriversReportAPIView, LoadsPaySummaryAPIView, TagViewSet, LoadTagViewSet,
                    BrokerBlacklistViewSet
                    )
from .internal_views import (InternalBookedLoadsCountView, InternalBookLoadView, InternalBrokerBlacklistBulkView, InternalBrokerResolveView, InternalBrokersBulkView, InternalBusyDriversView,
                             InternalCompanyProfileView, InternalDispatcherView, InternalRateConParseView)

router = SimpleRouter()
router.register(r'brokers', BrokersViewSet, basename="brokers") #checked
router.register(r'broker-blacklist', BrokerBlacklistViewSet, basename="broker-blacklist")
router.register(r'loads', LoadsViewSet, basename="loads") #checked
router.register(r'load-statuses', LoadStatusesViewSet, basename="load-statuses") #checked
router.register(r'load-stops', LoadStopsViewSet, basename="load-stops") #checked
router.register(r'payment-types', PaymentTypeViewSet, basename="payment-types") #checked
router.register(r'tags', TagViewSet, basename="tags") #checked
router.register(r'load-tags', LoadTagViewSet, basename="load-tags") #checked

urlpattern = ([
    path('loads/<int:pk>/history/', LoadHistoryByIdAPIView.as_view(), name='loadhistory-by-id'), #checked
    path('load-files/', LoadFilesAPIView.as_view(), name='upload-files'), #checked
    path('load-files/file/<int:file_id>/', LoadFilesAPIView.as_view(), name='file-detail'), #checked
    path('rate-con-upload/', RateConfirmationUploadView.as_view(), name='rate-con-upload'), #checked
    path('daily-dispatcher-report/', DailyReportByDispatchersAPIView.as_view(), name='daily-dispatcher-report'), #checked
    path('drivers-report/', DriversReportAPIView.as_view(), name='drivers-report'),
    path('loads-pay-summary/', LoadsPaySummaryAPIView.as_view(), name='loads-pay-summary'), #checked

    # email-agent (server-to-server, X-Internal-Secret)
    path('internal/company/<int:company_id>/', InternalCompanyProfileView.as_view(), name='internal-company'),
    path('internal/dispatcher/<int:user_id>/', InternalDispatcherView.as_view(), name='internal-dispatcher'),
    path('internal/brokers/resolve/', InternalBrokerResolveView.as_view(), name='internal-broker-resolve'),
    path('internal/brokers-bulk/', InternalBrokersBulkView.as_view(), name='internal-brokers-bulk'),
    path('internal/broker-blacklist-bulk/', InternalBrokerBlacklistBulkView.as_view(), name='internal-broker-blacklist-bulk'),
    path('internal/parse-ratecon/', InternalRateConParseView.as_view(), name='internal-parse-ratecon'),
    path('internal/book-load/', InternalBookLoadView.as_view(), name='internal-book-load'),
    path('internal/booked-loads-count/', InternalBookedLoadsCountView.as_view(), name='internal-booked-loads-count'),
    path('internal/busy-drivers/', InternalBusyDriversView.as_view(), name='internal-busy-drivers'),
])

urlpatterns = urlpattern + router.urls

