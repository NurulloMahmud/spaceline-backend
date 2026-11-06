from django.urls import path

from .views import (
    HubBidStatusView,
    HubDriverRemoveView,
    HubDriverView,
    HubInviteStatusView,
)

urlpatterns = [
    path("drivers/", HubDriverView.as_view(), name="hub-drivers"),
    path("drivers/remove/", HubDriverRemoveView.as_view(), name="hub-drivers-remove"),
    path("invites/status/", HubInviteStatusView.as_view(), name="hub-invites-status"),
    path("bid-status/", HubBidStatusView.as_view(), name="hub-bid-status"),
]
