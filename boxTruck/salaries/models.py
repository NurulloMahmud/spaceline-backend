"""Employee pay: a monthly fixed salary and a percentage of booked profit per employee, and one
company-wide daily bonus rule with a history, so a change never rewrites the months before it."""
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from users.models import CustomUser

ZERO = Decimal('0')


class EmployeeSalary(models.Model):
    """What an employee is paid from a given month on. A row is written only when something
    changes; the months after it carry the same numbers until the next row."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='salaries')
    month = models.DateField(help_text='The first day of the month this applies from')
    fixed_salary = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    profit_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal('100'))])
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'employee_salaries'
        verbose_name = 'Employee salary'
        verbose_name_plural = 'Employee salaries'
        unique_together = [('user', 'month')]
        ordering = ['-month', 'user_id']

    def __str__(self):
        return f'{self.user} from {self.month:%Y-%m}: {self.fixed_salary} + {self.profit_percentage}%'


class BonusRule(models.Model):
    """Pay `bonus_amount` for every full `profit_step` of profit an employee books in one day
    (Central time), from `effective_from` on. The latest rule on or before a day governs it."""
    effective_from = models.DateField(unique=True)
    profit_step = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    bonus_amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(ZERO)])
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bonus_rules'
        verbose_name = 'Daily bonus rule'
        verbose_name_plural = 'Daily bonus rules'
        ordering = ['-effective_from']

    def __str__(self):
        return f'from {self.effective_from}: {self.bonus_amount} per {self.profit_step}'


class BonusOverride(models.Model):
    """A daily bonus total set by hand for one employee and one month. It replaces the calculated
    total for that month only; the calculation still runs and is shown beside it."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='bonus_overrides')
    month = models.DateField(help_text='The first day of the month it applies to')
    amount = models.DecimalField(max_digits=20, decimal_places=2, validators=[MinValueValidator(ZERO)])
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'bonus_overrides'
        verbose_name = 'Bonus override'
        verbose_name_plural = 'Bonus overrides'
        unique_together = [('user', 'month')]
        ordering = ['-month', 'user_id']

    def __str__(self):
        return f'{self.user} {self.month:%Y-%m}: bonus {self.amount}'
