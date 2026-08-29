from django.urls import path, include
from rest_framework.routers import SimpleRouter
from .excel import ImportDriversView
from .views import (DriverBulkCreateHRView, DriverBulkCreateInviteView, DriverExistsCheckView,
                    DriverInviteSubmitView, DriverInviteDocumentUploadView,
                    DriverDocumentParseView,
                    GenerateDriverSignLinkView, DriverSignInfoView,
                    DriverStatusViewSet, DriverViewSet, DriverDropdownAPIView,
                    DriverFileViewSet, CompanyFileViewSet, GenerateInviteLinkView, VehicleFileViewSet,
                    VehicleViewSet, DriverCompanyViewSet, DepositViewSet, DriverListView, DriverCompanyModalView, DriverBulkView,
                    VehicleEquipmentViewSet, CompanyHistoryViewSet, VehicleHistoryViewSet, DriverHistoryViewSet, DepositHistoryViewSet,
                    DriverLocationByIDAPIView, DriverNearbyView, VehicleDropdownView,
                    DriverAssignmentView, UnassignedDriversAPIView
                    )

router = SimpleRouter()
router.register(r'driver-statuses', DriverStatusViewSet, basename='driver-statuses') #checked
router.register(r'drivers', DriverViewSet, basename='drivers') #checked
router.register(r'driver-files', DriverFileViewSet, basename='driver-files') #checked
router.register(r'company-files', CompanyFileViewSet, basename='company-files') #checked
router.register(r'vehicle-files', VehicleFileViewSet, basename='vehicle-files') #checked
router.register(r'vehicles', VehicleViewSet, basename='vehicles') #checked
router.register(r'driver-companies', DriverCompanyViewSet, basename='driver-companies') #checked
router.register(r'deposits', DepositViewSet, basename='deposits')
router.register(r'vehicle-equipments', VehicleEquipmentViewSet, basename='vehicle-equipments') # checked
router.register(r'company-history', CompanyHistoryViewSet, basename='company-history') # checked
router.register(r'vehicle-history', VehicleHistoryViewSet, basename='vehicle-history') # checked
router.register(r'driver-history', DriverHistoryViewSet, basename='driver-history') # checked
router.register(r'deposit-history', DepositHistoryViewSet, basename='deposit-history')

urlpattern = [
    path('driver-dropdown/', DriverDropdownAPIView.as_view(), name='driver-dropdown'), #checked
    path('import-drivers/', ImportDriversView.as_view(), name='import-drivers'), #checked
    path('driver-list/', DriverListView.as_view(), name='driver-list'), # checked
    path('driver-company-modal/', DriverCompanyModalView.as_view(), name='driver-company-modal'), # checked
    path('hiring/request/', DriverBulkCreateHRView.as_view(), name='driver-create-hr'),
    path('driver/invite/', DriverBulkCreateInviteView.as_view(), name='driver-create-invite'),
    path('driver/exists/', DriverExistsCheckView.as_view(), name='driver-exists-check'),
    path('driver/invite/submit/', DriverInviteSubmitView.as_view(), name='driver-invite-submit'),
    path('driver/invite/documents/', DriverInviteDocumentUploadView.as_view(), name='driver-invite-documents'),
    path('documents/parse/', DriverDocumentParseView.as_view(), name='driver-documents-parse'),
    path('invite-link/', GenerateInviteLinkView.as_view(), name='generate-invite-link'),
    path('driver/sign-link/', GenerateDriverSignLinkView.as_view(), name='generate-driver-sign-link'),
    path('driver/sign-info/', DriverSignInfoView.as_view(), name='driver-sign-info'),
    path('drivers-bulk/', DriverBulkView.as_view(), name='driver-bulk'),
    path('driver-location/<int:pk>/', DriverLocationByIDAPIView.as_view(), name='driver-location'),
    path('drivers-nearby/', DriverNearbyView.as_view(), name='driver-nearby'),
    path('vehicle-dropdown/', VehicleDropdownView.as_view(), name='vehicle-dropdown'),
    path('drivers-assign/', DriverAssignmentView.as_view(), name='drivers-assign'),
    path('drivers-unassigned/', UnassignedDriversAPIView.as_view(), name='drivers-unassigned'),
]

urlpatterns = router.urls + urlpattern
