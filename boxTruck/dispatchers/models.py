from django.db import models
from users.models import Company, CustomUser

# Create your models here.
class DispatchSalary(models.Model):
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)
    min_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    max_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    percentage = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatch_salary_created_by')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatch_salary_updated_by')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dispatch Salary"
        verbose_name_plural = "Dispatch Salaries"
        db_table = "dispatch_salaries"

    def __str__(self):
        return f"{self.created_at}"
    

class DispatchBonus(models.Model):
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True)
    min_daily_profit = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    max_daily_profit = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    bonus_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatch_bonus_created_by')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='dispatch_bonus_updated_by')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dispatch Bonus"
        verbose_name_plural = "Dispatch Bonuses"
        db_table = "dispatch_bonuses"

    def __str__(self):
        return f"{self.created_at}"
