from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from billing.models import Load
from payroll.models import Deduction
from users.models import CustomUser


ZERO = Decimal('0.00')


class Payout(models.Model):
    """The driver side of a load: what the driver is paid for it.

    `gross` is what the driver earns on the load (the load's driver pay unless accounting
    changed it). The amount actually paid is gross minus what this payout takes from the
    driver's expenses (PayoutExpense), never stored, always derived, so the two cannot drift.

    `quick_pay_fee` is what Quick Pay moved from the driver to the company. It has already come
    off both `gross` and the load's driver pay, so the load's profit includes it, and it is kept
    so the move can be undone when the payout leaves Quick Pay.
    """
    PENDING, CONFIRMED, PAID = 'pending', 'confirmed', 'paid'
    STATUSES = [(PENDING, 'Pending'), (CONFIRMED, 'Confirmed'), (PAID, 'Paid')]
    QUICK_PAY = 'Quick Pay'
    PAYMENT_TYPES = [QUICK_PAY, 'Regular Pay', 'Zelle in Advance']

    load = models.OneToOneField(Load, on_delete=models.CASCADE, related_name='payout')
    gross = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO)
    quick_pay_fee = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO)
    payment_type = models.CharField(max_length=50, default='Regular Pay')
    status = models.CharField(max_length=20, choices=STATUSES, default=PENDING)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reopened_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payouts'
        ordering = ['-id']

    def __str__(self):
        return f'Payout for load {self.load_id}'

    @property
    def applied_total(self):
        return self.expenses.aggregate(s=Coalesce(Sum('amount'), ZERO))['s']

    @property
    def amount(self):
        return max(ZERO, self.gross - self.applied_total)


class PayoutExpense(models.Model):
    """What one payout takes from one driver expense. The expense's own amount never changes."""
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name='expenses')
    deduction = models.ForeignKey(Deduction, on_delete=models.CASCADE, related_name='payout_links')
    amount = models.DecimalField(max_digits=20, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payout_expenses'
        unique_together = [('payout', 'deduction')]


class PayoutNote(models.Model):
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name='notes')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payout_notes'
        ordering = ['created_at', 'id']


class PayoutHistory(models.Model):
    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name='history')
    changed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payout_history'
        ordering = ['created_at', 'id']


class Invoice(models.Model):
    """The broker side of a load: what the factor collects from the debtor.

    `original` is the carrier pay the load was billed at; `amount` is what is expected or
    was received. An amount below the original is a short pay, whatever the status.
    """
    GOOD, INVOICED, PAID = 'good', 'invoiced', 'paid'
    STATUSES = [(GOOD, 'Good to invoice'), (INVOICED, 'Invoiced'), (PAID, 'Paid')]

    load = models.OneToOneField(Load, on_delete=models.CASCADE, related_name='invoice')
    original = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO)
    amount = models.DecimalField(max_digits=20, decimal_places=2, default=ZERO)
    status = models.CharField(max_length=20, choices=STATUSES, default=GOOD)
    invoiced_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'invoices'
        ordering = ['-id']

    def __str__(self):
        return f'Invoice for load {self.load_id}'

    @property
    def short(self):
        return self.amount < self.original


class InvoiceNote(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='notes')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'invoice_notes'
        ordering = ['created_at', 'id']


class InvoiceHistory(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='history')
    changed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    description = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'invoice_history'
        ordering = ['created_at', 'id']


# --- Expenses (payroll.Deduction) as debts ---------------------------------------------------

def annotate_debt(qs):
    """settled / held / owed on a Deduction queryset.

    A deduction marked paid before this page existed counts as settled in full; otherwise
    settled is what paid payouts took, and held is what open payouts are taking.
    """
    return qs.annotate(
        settled_links=Coalesce(Sum('payout_links__amount', filter=Q(payout_links__payout__status=Payout.PAID)), ZERO),
        held_sum=Coalesce(Sum('payout_links__amount', filter=~Q(payout_links__payout__status=Payout.PAID)), ZERO),
    ).annotate(
        settled_sum=models.Case(models.When(paid=True, then=models.F('amount')), default=models.F('settled_links')),
    ).annotate(
        owed_sum=models.F('amount') - models.F('settled_sum'),
    )


def debt_of(deduction):
    """[settled, held, owed] for one deduction, from its links."""
    links = deduction.payout_links.select_related('payout')
    settled = sum((l.amount for l in links if l.payout.status == Payout.PAID), ZERO)
    held = sum((l.amount for l in links if l.payout.status != Payout.PAID), ZERO)
    if deduction.paid:
        settled = deduction.amount
    return settled, held, max(ZERO, deduction.amount - settled)


def available_for(deduction, payout):
    """How much of a deduction this payout may still take: what is not settled or held elsewhere."""
    settled, _, _ = debt_of(deduction)
    held_elsewhere = sum((l.amount for l in deduction.payout_links.select_related('payout') if l.payout.status != Payout.PAID and l.payout_id != payout.id), ZERO)
    return max(ZERO, deduction.amount - settled - held_elsewhere)


def open_deductions(driver):
    return [d for d in annotate_debt(Deduction.objects.filter(driver=driver, is_deleted=False)).order_by('date', 'id') if d.owed_sum > 0]
