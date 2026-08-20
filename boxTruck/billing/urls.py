from django.urls import path
from rest_framework.routers import SimpleRouter
from .views import (BrokersViewSet, LoadStopsViewSet,
                    LoadsViewSet, LoadStatusesViewSet, BatchListView, PaymentTypeViewSet, BestProfitLoadsAPIView,
                    LoadFilesAPIView, LoadHistoryByIdAPIView, BrokerListView, LoadsCountByProcessView, RateConfirmationUploadView,
                    BatchViewSet, BatchLoadViewSet, BatchLoadDropDown, MultipleBatchLoadCreateView, LoadByDriverForStatementView, BatchLoadByIDListAPIView,
                    GenerateCSVView, DailyReportByDispatchersAPIView, LoadsPaySummaryAPIView, BrokerImportAPIView, TagViewSet, LoadTagViewSet
                    )
from .internal_views import (InternalBookLoadView, InternalBrokerResolveView, InternalBrokersBulkView, InternalBusyDriversView,
                             InternalCompanyProfileView, InternalDispatcherView, InternalRateConParseView)

router = SimpleRouter()
router.register(r'brokers', BrokersViewSet, basename="brokers") #checked
router.register(r'loads', LoadsViewSet, basename="loads") #checked
router.register(r'load-statuses', LoadStatusesViewSet, basename="load-statuses") #checked
router.register(r'load-stops', LoadStopsViewSet, basename="load-stops") #checked
router.register(r'batch', BatchViewSet, basename="batch") #checked
router.register(r'batch-loads', BatchLoadViewSet, basename="batch-load") #checked
router.register(r'payment-types', PaymentTypeViewSet, basename="payment-types") #checked
router.register(r'tags', TagViewSet, basename="tags") #checked
router.register(r'load-tags', LoadTagViewSet, basename="load-tags") #checked

urlpattern = ([
    path('loads/<int:pk>/history/', LoadHistoryByIdAPIView.as_view(), name='loadhistory-by-id'), #checked
    path('load-files/', LoadFilesAPIView.as_view(), name='upload-files'), #checked
    path('load-files/file/<int:file_id>/', LoadFilesAPIView.as_view(), name='file-detail'), #checked
    path('broker-list/', BrokerListView.as_view(), name='broker-list'), #checked
    path('rate-con-upload/', RateConfirmationUploadView.as_view(), name='rate-con-upload'), #checked
    path('batch-load-dropdown/', BatchLoadDropDown.as_view(), name='batch-load-dropdown'), #checked
    path('multiple-batch-load-create/', MultipleBatchLoadCreateView.as_view(), name='multiple-batch-load-create'), #checked
    path('driver-load-dropdown/', LoadByDriverForStatementView.as_view(), name='load-driver-statement-dropdown'), #checked
    path('batch/<int:pk>/loads/', BatchLoadByIDListAPIView.as_view(), name='batch-loads-by-id'), #checked
    path('loads-by-statuses/', LoadsCountByProcessView.as_view(), name='loads-by-statuses'), #checked
    path('batch-list/', BatchListView.as_view(), name='batch-list'), #checked
    path('factoring-csv/', GenerateCSVView.as_view(), name='factoring-csv'), #need test credentials
    path('daily-dispatcher-report/', DailyReportByDispatchersAPIView.as_view(), name='daily-dispatcher-report'), #checked
    path('loads-pay-summary/', LoadsPaySummaryAPIView.as_view(), name='loads-pay-summary'), #checked
    path('brokers-import/', BrokerImportAPIView.as_view(), name='brokers-import'), #checked
    path('best-profitable-load/', BestProfitLoadsAPIView.as_view(), name='best-profitable-load'),

    # email-agent (server-to-server, X-Internal-Secret)
    path('internal/company/<int:company_id>/', InternalCompanyProfileView.as_view(), name='internal-company'),
    path('internal/dispatcher/<int:user_id>/', InternalDispatcherView.as_view(), name='internal-dispatcher'),
    path('internal/brokers/resolve/', InternalBrokerResolveView.as_view(), name='internal-broker-resolve'),
    path('internal/brokers-bulk/', InternalBrokersBulkView.as_view(), name='internal-brokers-bulk'),
    path('internal/parse-ratecon/', InternalRateConParseView.as_view(), name='internal-parse-ratecon'),
    path('internal/book-load/', InternalBookLoadView.as_view(), name='internal-book-load'),
    path('internal/busy-drivers/', InternalBusyDriversView.as_view(), name='internal-busy-drivers'),
])

urlpatterns = urlpattern + router.urls

