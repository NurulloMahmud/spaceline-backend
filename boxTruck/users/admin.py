from django.contrib import admin
from .models import Company, CustomUser, PasswordReset, Department, Page, UserPageAccess


# Register your models here.
admin.site.register(Company)
admin.site.register(PasswordReset)
admin.site.register(CustomUser)
admin.site.register(Department)
admin.site.register(Page)
admin.site.register(UserPageAccess)
