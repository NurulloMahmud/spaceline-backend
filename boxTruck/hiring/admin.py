from django.contrib import admin
from .models import Driver, DriverStatus

# Register your models here.
admin.site.register(Driver)
admin.site.register(DriverStatus)