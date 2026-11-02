from django.contrib import admin

from .models import BonusOverride, BonusRule, EmployeeSalary


@admin.register(EmployeeSalary)
class EmployeeSalaryAdmin(admin.ModelAdmin):
    list_display = ('user', 'month', 'fixed_salary', 'profit_percentage', 'updated_by', 'last_updated')
    list_filter = ('month',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name')


@admin.register(BonusRule)
class BonusRuleAdmin(admin.ModelAdmin):
    list_display = ('effective_from', 'profit_step', 'bonus_amount', 'created_by', 'created_at')


@admin.register(BonusOverride)
class BonusOverrideAdmin(admin.ModelAdmin):
    list_display = ('user', 'month', 'amount', 'updated_by', 'last_updated')
    list_filter = ('month',)
    search_fields = ('user__username', 'user__first_name', 'user__last_name')
