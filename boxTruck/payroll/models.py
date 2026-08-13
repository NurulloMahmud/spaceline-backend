from django.db import models

from billing.models import Load
from hiring.models import Driver
from users.models import Company, CustomUser

# Create your models here.
class StatementStatus(models.Model):
    name = models.CharField(max_length=150)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Statement Statuses"
        verbose_name = "Statement Status"
        db_table = 'statement_statuses'
        ordering = ['-id']


class DeductionType(models.Model):
    name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Deduction Types"
        verbose_name = "Deduction Type"
        db_table = 'deduction_types'


class Deduction(models.Model):
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    paid = models.BooleanField(default=False)
    date = models.DateField(null=True, blank=True)
    type = models.ForeignKey(DeductionType, on_delete=models.CASCADE, null=True, blank=True, related_name='deductions')
    fee = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    last_updated = models.DateField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    def __str__(self):
        return self.driver.full_name

    class Meta:
        verbose_name_plural = "Deductions"
        verbose_name = "Deduction"
        db_table = 'deductions'
        ordering = ['-date']


class DeductionHistory(models.Model):
    deduction = models.ForeignKey(Deduction, on_delete=models.CASCADE)
    changed_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    description = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.deduction.driver.full_name

    class Meta:
        verbose_name_plural = "Deduction Histories"
        verbose_name = "Deduction History"
        db_table = 'deduction_histories'


class Statement(models.Model):
    start_date = models.DateField()
    end_date = models.DateField()
    company = models.ForeignKey(Company, on_delete=models.CASCADE)
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, null=True, blank=True)
    created_by = models.ForeignKey(CustomUser, on_delete=models.CASCADE, null=True, blank=True, related_name='created_by')
    gross_amount = models.DecimalField(max_digits=20, decimal_places=2, null=True, blank=True)
    status = models.ForeignKey(StatementStatus, on_delete=models.CASCADE, null=True, blank=True)
    final = models.BooleanField(default=False)
    pdf_file = models.FileField(upload_to='payroll/statement/', null=True, blank=True)
    week_number = models.IntegerField(null=True, blank=True)
    telegram_send = models.DateTimeField(null=True, blank=True)
    email_send = models.DateTimeField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    created_at = models.DateField(auto_now_add=True)
    last_updated = models.DateField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.pk:
            old_instance = Statement.objects.get(pk=self.pk)
            old_file = old_instance.pdf_file
            new_file = self.pdf_file
            if old_file and old_file != new_file:
                old_file.delete(save=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.driver.name

    class Meta:
        verbose_name_plural = "Statements"
        verbose_name = "Statement"
        db_table = 'statements'
        ordering = ['-start_date']


class StatementLoad(models.Model):
    statement = models.ForeignKey(Statement, on_delete=models.CASCADE)
    load = models.ForeignKey(Load, on_delete=models.CASCADE)
    created_at = models.DateField(auto_now_add=True)
    last_updated = models.DateField(auto_now=True)

    def __str__(self):
        return self.statement.driver.name

    class Meta:
        verbose_name_plural = "Statement Loads"
        verbose_name = "Statement Load"
        db_table = 'statement_loads'
        ordering = ['-id']


class StatementDeduction(models.Model):
    deduction = models.ForeignKey(Deduction, on_delete=models.CASCADE)
    statement = models.ForeignKey(Statement, on_delete=models.CASCADE)
    created_at = models.DateField(auto_now_add=True)
    last_updated = models.DateField(auto_now=True)

    def __str__(self):
        return self.deduction.driver.full_name

    class Meta:
        verbose_name_plural = "Statement Deductions"
        verbose_name = "Statement Deduction"
        db_table = 'statement_deductions'
        ordering = ['-id']

