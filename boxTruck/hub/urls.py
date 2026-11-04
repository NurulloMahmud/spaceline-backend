from django.urls import path

from .views import CustomAlertsView, HubBidStatusView, HubDriverRemoveView, HubDriverView

urlpatterns = [
    path('drivers/', HubDriverView.as_view(), name='hub-drivers'),
    path('drivers/remove/', HubDriverRemoveView.as_view(), name='hub-drivers-remove'),
    path('bid-status/', HubBidStatusView.as_view(), name='hub-bid-status'),
    path('custom-alerts/', CustomAlertsView.as_view(), name='custom-alerts'),
]
