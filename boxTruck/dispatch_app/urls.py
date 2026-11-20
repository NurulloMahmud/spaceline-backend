from django.urls import path

from .views import (
    DevicesView,
    DeviceView,
    InternalNotifyView,
    LinkCodeView,
    RevokeView,
    SessionView,
)

urlpatterns = [
    path("link-code/", LinkCodeView.as_view(), name="dispatch-link-code"),
    path("device/", DeviceView.as_view(), name="dispatch-device"),
    path("devices/", DevicesView.as_view(), name="dispatch-devices"),
    path("devices/revoke/", RevokeView.as_view(), name="dispatch-devices-revoke"),
    path("session/", SessionView.as_view(), name="dispatch-session"),
    path("internal/notify/", InternalNotifyView.as_view(), name="dispatch-internal-notify"),
]
