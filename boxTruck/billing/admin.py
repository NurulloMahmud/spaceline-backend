from django.contrib import admin
from .models import (Broker, BrokerBlacklist, LoadStatus, Load, LoadHistory, LoadFile, LoadStop,
                     Batch, BatchLoad
                     )


# Register your models here.
admin.site.register(Broker)
admin.site.register(LoadStatus)
@admin.register(Load)
class LoadAdmin(admin.ModelAdmin):
    search_fields = ['load_number', 'shipment']
    list_filter = ['booked_by__username', 'company__name', 'driver__full_name']
admin.site.register(LoadHistory)
admin.site.register(LoadFile)
@admin.register(LoadStop)
class LoadStopAdmin(admin.ModelAdmin):
    search_fields = ['load__load_number']
    list_filter = ['order']
admin.site.register(Batch)
admin.site.register(BatchLoad)


@admin.register(BrokerBlacklist)
class BrokerBlacklistAdmin(admin.ModelAdmin):
    list_display = ['company', 'name', 'mc', 'created_by', 'created_at']
    list_filter = ['company__name']
    search_fields = ['name', 'mc', 'company__name', 'created_by__username']
