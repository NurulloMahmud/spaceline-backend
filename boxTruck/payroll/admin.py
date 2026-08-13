from django.contrib import admin
from .models import (Statement, StatementLoad, StatementStatus, StatementDeduction, Deduction, DeductionHistory)

# Register your models here.
@admin.register(Statement)
class StatementAdmin(admin.ModelAdmin):
    list_display = ['driver', 'company', 'start_date', 'end_date', 'gross_amount', 'status', 'final']
    list_filter = ['status', 'final', 'company']
    search_fields = ['driver__name']


@admin.register(StatementLoad)
class StatementLoadAdmin(admin.ModelAdmin):
    list_display = ['statement', 'load', 'created_at']
    search_fields = ['statement__driver__full_name', 'load__id']


@admin.register(StatementStatus)
class StatementStatusAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']

@admin.register(Deduction)
class DeductionAdmin(admin.ModelAdmin):
    list_display = ['driver', 'amount', 'fee', 'paid', 'date', 'type']
    list_filter = ['paid', 'type']
    search_fields = ['driver__full_name', 'note']

@admin.register(DeductionHistory)
class DeductionHistoryAdmin(admin.ModelAdmin):
    list_display = ['deduction', 'changed_by', 'created_at']
    search_fields = ['deduction__driver__full_name', 'changed_by__username', 'description']

@admin.register(StatementDeduction)
class StatementDeductionAdmin(admin.ModelAdmin):
    list_display = ['statement', 'deduction', 'created_at']
    search_fields = ['statement__driver__full_name', 'deduction__driver__full_name']
