from django.contrib import admin

from .models import DispatchDevice


@admin.register(DispatchDevice)
class DispatchDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "platform",
        "device_label",
        "created_at",
        "last_seen_at",
        "revoked_at",
        "revoked_reason",
    )
    list_filter = ("platform", "revoked_reason")
    search_fields = ("user__username", "user__first_name", "user__last_name", "device_label")
    readonly_fields = [f.name for f in DispatchDevice._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False
