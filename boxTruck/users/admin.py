from django.contrib import admin
from .models import Company, CustomUser, PasswordReset, Department, Page, UserPageAccess


# Register your models here.
admin.site.register(Company)
admin.site.register(PasswordReset)
admin.site.register(Department)
admin.site.register(Page)
admin.site.register(UserPageAccess)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    """The user list shows each person's theme, as the app last reported it."""
    list_display = ('username', 'first_name', 'last_name', 'department', 'is_active', 'theme', 'theme_seen_at')
    list_filter = ('theme', 'is_active', 'department')
    search_fields = ('username', 'first_name', 'last_name')
