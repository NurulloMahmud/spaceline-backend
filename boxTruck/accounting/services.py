"""What the accounting records do on their own: creation with the load, taking open expenses,
and following the load's pay and status changes."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from billing.models import Load, LoadHistory
from payroll.models import Deduction, DeductionHistory, DeductionType

from .models import (
    Invoice, InvoiceHistory, Payout, PayoutExpense, PayoutHistory, ZERO, available_for, debt_of, open_deductions,
)

PAY_TYPE_ALIASES = {
    'quick pay': 'Quick Pay', 'quickpay': 'Quick Pay', 'regular pay': 'Regular Pay', 'standard pay': 'Regular Pay',
    'zelle in advance': 'Zelle in Advance', 'zelle': 'Zelle in Advance',
}


def money(v):
    v = Decimal(v or 0)
    return f'${v:,.2f}' if v >= 0 else f'-${-v:,.2f}'


def payment_type_of(load):
    return PAY_TYPE_ALIASES.get(str(load.payment_type or '').strip().lower(), 'Regular Pay')


def shipment_of(load):
    return f'SH - {load.shipment}' if load.shipment else f'load {load.id}'


def log_payout(payout, user, text):
    PayoutHistory.objects.create(payout=payout, changed_by=user, description=text)


def log_invoice(invoice, user, text):
    InvoiceHistory.objects.create(invoice=invoice, changed_by=user, description=text)


def take_open_expenses(payout, user=None, exclude=None):
    """Put the driver's open expenses on this payout, oldest first, up to what it can carry.

    Returns the links made. Used when the payout is created and when a new expense arrives.
    """
    if payout.status == Payout.PAID or not payout.load.driver_id:
        return []
    taken = []
    room = payout.amount
    for d in open_deductions(payout.load.driver):
        if room <= 0:
            break
        if exclude is not None and d.id == exclude:
            continue
        if payout.expenses.filter(deduction=d).exists():
            continue
        can = min(available_for(d, payout), room)
        if can <= 0:
            continue
        link = PayoutExpense.objects.create(payout=payout, deduction=d, amount=can)
        room -= can
        _, _, owed = debt_of(d)
        left = owed - can
        log_payout(payout, user, f'{d.type.name if d.type else "Expense"}: took {money(can)} of {money(d.amount)}' + (f'; {money(left)} stays open' if left > 0 else ''))
        taken.append(link)
    return taken


def take_expense_everywhere(deduction, user=None, exclude_payout=None):
    """A new or raised expense goes onto the driver's open payouts, oldest pick-up first.

    A payout that already takes part of it takes more if it has room.
    """
    taken = []
    name = deduction.type.name if deduction.type_id else 'Expense'
    payouts = Payout.objects.filter(load__driver=deduction.driver).exclude(status=Payout.PAID).exclude(id=exclude_payout or 0)
    for p in payouts.select_related('load').order_by('load__pickup_date', 'id'):
        _, held, owed = debt_of(deduction)
        remaining = owed - held
        if remaining <= 0:
            break
        room = min(available_for(deduction, p), p.amount, remaining)
        if room <= 0:
            continue
        link = PayoutExpense.objects.filter(payout=p, deduction=deduction).first()
        if link:
            link.amount += room
            link.save(update_fields=['amount'])
        else:
            link = PayoutExpense.objects.create(payout=p, deduction=deduction, amount=room)
        left = remaining - room
        log_payout(p, user, f'{name}: took {money(room)} of {money(deduction.amount)} (new expense)' + (f'; {money(left)} stays open' if left > 0 else ''))
        taken.append(link)
    return taken


@transaction.atomic
def create_for_load(load, user=None, take_expenses=True):
    """Both records for a booked load. Safe to call twice: existing records are kept."""
    payout, made_p = Payout.objects.get_or_create(load=load, defaults={'gross': load.driver_pay or ZERO, 'payment_type': payment_type_of(load)})
    if made_p:
        log_payout(payout, user, f'Created from load {shipment_of(load)} with the driver pay {money(load.driver_pay)}')
        if take_expenses:
            take_open_expenses(payout, user)
    invoice, made_i = Invoice.objects.get_or_create(load=load, defaults={'original': load.carrier_pay or ZERO, 'amount': load.carrier_pay or ZERO})
    if made_i:
        log_invoice(invoice, user, f'Created from load {shipment_of(load)} with the carrier pay {money(load.carrier_pay)}')
    return payout, invoice


def follow_load_change(load, old, user=None):
    """The load's driver pay, carrier pay or status changed on the Loads page: keep both records in step."""
    payout = Payout.objects.filter(load=load).first()
    invoice = Invoice.objects.filter(load=load).first()
    if old.get('driver_pay') != load.driver_pay and payout and payout.status != Payout.PAID and payout.gross == (old.get('driver_pay') or ZERO):
        Payout.objects.filter(pk=payout.pk).update(gross=load.driver_pay or ZERO)
        log_payout(payout, user, f'Driver pay on the load {money(old.get("driver_pay"))} to {money(load.driver_pay)}; payout follows it')
    if old.get('carrier_pay') != load.carrier_pay and invoice and invoice.status != Invoice.PAID and invoice.amount == invoice.original == (old.get('carrier_pay') or ZERO):
        Invoice.objects.filter(pk=invoice.pk).update(original=load.carrier_pay or ZERO, amount=load.carrier_pay or ZERO)
        log_invoice(invoice, user, f'Carrier pay on the load {money(old.get("carrier_pay"))} to {money(load.carrier_pay)}; invoice follows it')
    if old.get('status_id') != load.status_id:
        text = f'Load status {old.get("status_name") or "-"} to {load.status.name if load.status else "-"}'
        if payout:
            log_payout(payout, user, text)
        if invoice:
            log_invoice(invoice, user, text)


def set_load_status(load, status, user):
    """Load status changed from the Accounting page: the load changes, with its own history."""
    old_name = load.status.name if load.status else '-'
    Load.objects.filter(pk=load.pk).update(status=status, updated_by=user)
    LoadHistory.objects.create(load=load, changed_by=user, description=f'Load Updated | Status: {old_name} -> {status.name}')
    load.status = status
    text = f'Load status {old_name} to {status.name}'
    if hasattr(load, 'payout'):
        log_payout(load.payout, user, text)
    if hasattr(load, 'invoice'):
        log_invoice(load.invoice, user, text)


def raise_driver_pay(load, new_gross, user, payout):
    old = load.driver_pay
    Load.objects.filter(pk=load.pk).update(driver_pay=new_gross, updated_by=user)
    LoadHistory.objects.create(load=load, changed_by=user, description=f'Load Updated | Driver Pay: {old} -> {new_gross} (from payout)')
    load.driver_pay = new_gross


def overpayment_expense(load, over, user, payout):
    kind, _ = DeductionType.objects.get_or_create(name='Overpayment')
    d = Deduction.objects.create(driver=load.driver, amount=over, type=kind, date=timezone.localdate(), created_by=user,
                                 note=f'From payout {shipment_of(load)}: paid {money(over)} over the driver pay')
    DeductionHistory.objects.create(deduction=d, changed_by=user, description='Deduction Created (overpayment from payout)')
    take_expense_everywhere(d, user, exclude_payout=payout.id)
    return d
