"""Replace the accounting records with what the team's two Google Sheets say.

    manage.py import_accounting_sheets --payouts payouts.csv --factoring factoring.csv [--dry-run]

The payouts sheet ("SPACE LINE LLC1": DATE, Invoice#, Load #, Driver names, Payout, Status,
pay type, note) is the truth for the driver side; the factoring sheet ("Spaceline aging":
INVOICE, REF#, DEBTOR, ..., AMOUNT, STATUS, UPDATE, comment, payment details) for the broker
side. Every payout and invoice is deleted first, then one of each is created for every load
that is not rejected, and the sheets' rows are applied to the loads they name. A load the
sheets do not mention keeps a fresh pending payout and a good-to-invoice invoice. The report
at the end lists those loads, oldest first, so the ones that should have been in the sheets
stand out. Driver expenses (deductions) are left alone.
"""
import csv
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounting.models import Invoice, InvoiceNote, Payout, PayoutNote, ZERO
from accounting.services import create_for_load, log_invoice, log_payout, money
from billing.models import Load

PAY_TYPES = {'quickpay': 'Quick Pay', 'quick pay': 'Quick Pay', 'standard pay': 'Regular Pay', 'regular pay': 'Regular Pay', 'zelle in advance': 'Zelle in Advance'}
INVOICE_STATUS = {'1': Invoice.GOOD, '2': Invoice.INVOICED, '3': Invoice.INVOICED, '4': Invoice.PAID}
STATUS_RANK = {Invoice.GOOD: 0, Invoice.INVOICED: 1, Invoice.PAID: 2}


def amount_of(text):
    s = re.sub(r'[^\d.\-]', '', str(text or ''))
    try:
        return Decimal(s) if s else None
    except InvalidOperation:
        return None


def day(text, formats):
    """An aware datetime at noon, so the day is the same in every US zone, or None."""
    s = str(text or '').strip()
    for f in formats:
        try:
            return timezone.make_aware(datetime.strptime(s, f).replace(hour=12))
        except ValueError:
            continue
    return None


MDY = ('%m/%d/%Y', '%m/%d/%y')
DMY = ('%d/%m/%Y', '%d/%m/%y')


def shipment_in(text):
    """The shipment number in "SH - 10024"; None when the text is not exactly that, since a
    typo like "SH - 100246912" names some other load."""
    m = re.fullmatch(r'\s*SH\s*-\s*(\d{5})\s*', str(text or ''))
    return int(m.group(1)) if m else None


def paid_details(text):
    """The factoring sheet's payment column: "SH - 10024  U6700917  09/04/2026  09/23/2026  EFT_19198260".
    Returns (invoiced, paid, reference)."""
    parts = [p for p in re.split(r'\s+', str(text or '').strip()) if p]
    dates = [day(p, MDY) for p in parts]
    where = [i for i, d in enumerate(dates) if d]
    invoiced = dates[where[0]] if where else None
    paid = dates[where[1]] if len(where) > 1 else None
    # The payment reference is the token right after the paid date; anything after it is
    # the debtor's name repeated.
    ref = parts[where[1] + 1] if len(where) > 1 and where[1] + 1 < len(parts) else ''
    return invoiced, paid, ref


class Command(BaseCommand):
    help = 'Replace payouts and invoices with the two accounting sheets'

    def add_arguments(self, parser):
        parser.add_argument('--payouts', required=True)
        parser.add_argument('--factoring', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        pay_rows = self.read(opts['payouts'], header_contains='Invoice#')
        fac_rows = self.read(opts['factoring'], header_contains='INVOICE')
        loads = list(Load.objects.exclude(status__name__iexact='Rejected').select_related('status', 'driver', 'broker').order_by('shipment', 'id'))
        by_sh = {l.shipment: l for l in loads if l.shipment}
        by_ln = defaultdict(list)
        for l in loads:
            if l.load_number:
                by_ln[l.load_number.strip().lower()].append(l)

        def load_for(sh_text, ref_text):
            sh = shipment_in(sh_text)
            if sh in by_sh:
                return by_sh[sh]
            # A mistyped shipment: the reference number decides, when it names one load.
            for part in re.split(r'[\s/]+', str(ref_text or '')):
                hits = by_ln.get(part.strip().lower())
                if hits and len(hits) == 1:
                    return hits[0]
            return None

        pay_by_load, pay_lost = defaultdict(list), []
        for r in pay_rows:
            l = load_for(r[1], r[2])
            (pay_by_load[l.id] if l else pay_lost).append(r)
        fac_by_load, fac_lost = defaultdict(list), []
        for r in fac_rows:
            l = load_for(r[0], r[1])
            (fac_by_load[l.id] if l else fac_lost).append(r)

        if opts['dry_run']:
            self.report(loads, pay_by_load, fac_by_load, pay_lost, fac_lost, dry=True)
            return

        with transaction.atomic():
            Payout.objects.all().delete()
            Invoice.objects.all().delete()
            for l in loads:
                payout, invoice = create_for_load(l, take_expenses=False)
                log_payout(payout, None, 'Created by the sheet import')
                log_invoice(invoice, None, 'Created by the sheet import')
                if l.id in pay_by_load:
                    self.apply_payout(payout, l, pay_by_load[l.id])
                if l.id in fac_by_load:
                    self.apply_invoice(invoice, l, fac_by_load[l.id])
        self.report(loads, pay_by_load, fac_by_load, pay_lost, fac_lost, dry=False)

    def read(self, path, header_contains):
        try:
            rows = list(csv.reader(open(path, newline='', encoding='utf-8-sig')))
        except OSError as e:
            raise CommandError(f'cannot read {path}: {e}')
        start = next((i for i, r in enumerate(rows) if any(c.strip() == header_contains for c in r)), None)
        if start is None:
            raise CommandError(f'{path}: no header row containing {header_contains!r}')
        out = []
        for r in rows[start + 1:]:
            r = [c.strip() for c in r] + [''] * 13
            if r[0] or r[1]:
                out.append(r)
        return out

    def apply_payout(self, payout, load, rows):
        gross = sum((amount_of(r[4]) or ZERO for r in rows), ZERO)
        paid_rows = [r for r in rows if r[5].upper() == 'TRUE']
        dates = [d for d in (day(r[0], MDY) for r in rows) if d]
        pay_type = next((PAY_TYPES.get(r[6].lower()) for r in rows if PAY_TYPES.get(r[6].lower())), payout.payment_type)
        payout.gross = gross
        payout.payment_type = pay_type
        if paid_rows:
            payout.status = Payout.PAID
            payout.confirmed_at = payout.paid_at = max(dates) if dates else timezone.now()
        payout.save()
        lines = ', '.join(f'{money(amount_of(r[4]) or ZERO)} on {r[0]} ({r[6] or "no pay type"}{", paid" if r[5].upper() == "TRUE" else ", not paid"})' for r in rows)
        log_payout(payout, None, f'Imported from the payouts sheet: {lines}')
        if (load.driver_pay or ZERO) != gross:
            log_payout(payout, None, f'Load driver pay {money(load.driver_pay)}; the sheet paid {money(gross)}')
        driver = (load.driver.full_name if load.driver_id else '').strip().lower()
        for r in rows:
            name = r[3].split('(')[0].strip()
            if name and driver and name.lower().split()[0] not in driver:
                PayoutNote.objects.create(payout=payout, text=f'Sheet names the driver as {r[3]}')
        for note in dict.fromkeys(r[7] for r in rows if r[7]):
            PayoutNote.objects.create(payout=payout, text=note)

    def apply_invoice(self, invoice, load, rows):
        def status_of(r):
            return INVOICE_STATUS.get(r[6][:1], Invoice.GOOD)
        best = max(rows, key=lambda r: STATUS_RANK[status_of(r)])
        status = status_of(best)
        amount = amount_of(best[5])
        if amount is not None:
            invoice.amount = amount
            invoice.original = load.carrier_pay or amount
            if amount > invoice.original:
                invoice.original = amount
        updated = day(best[7], DMY)
        invoiced_at, paid_at, ref = paid_details(best[9])
        invoice.status = status
        if status in (Invoice.INVOICED, Invoice.PAID):
            invoice.invoiced_at = invoiced_at or updated or timezone.now()
        if status == Invoice.PAID:
            invoice.paid_at = paid_at or updated or invoice.invoiced_at
        invoice.save()
        log_invoice(invoice, None, f'Imported from the factoring sheet: {best[6] or "no status"}, {money(invoice.amount)}, debtor {best[2] or "-"}, updated {best[7] or "-"}')
        if ref:
            InvoiceNote.objects.create(invoice=invoice, text=f'Paid, reference {ref}')
        for r in rows:
            if r[8]:
                InvoiceNote.objects.create(invoice=invoice, text=r[8] if not re.fullmatch(r'\d{1,2}/\d{1,2}/\d{2,4}', r[8]) else f'Follow-up {r[8]}')
        for r in rows:
            if r is not best:
                log_invoice(invoice, None, f'Another sheet row: {r[6] or "no status"}, {r[5]}, ref {r[1]}')

    def report(self, loads, pay_by_load, fac_by_load, pay_lost, fac_lost, dry):
        w = self.stdout.write
        w(f'{"Would import" if dry else "Imported"}: {len(loads)} loads, {sum(len(v) for v in pay_by_load.values())} payout rows on {len(pay_by_load)} loads, {sum(len(v) for v in fac_by_load.values())} factoring rows on {len(fac_by_load)} loads')
        for r in pay_lost:
            w(f'  payout row not matched to a load: {r[1]} / {r[2]} / {r[3]}')
        for r in fac_lost:
            w(f'  factoring row not matched to a load: {r[0]} / {r[1]} / {r[2]}')
        today = timezone.now()

        def age(l):
            t = l.delivered_at or l.drop_date or l.pickup_date or l.created_at
            return max(0, (today - t).days) if t else None
        for title, found in (('payouts sheet', pay_by_load), ('factoring sheet', fac_by_load)):
            missing = sorted((l for l in loads if l.id not in found), key=lambda l: (-(age(l) or 0), l.shipment or 0))
            w(f'\n{len(missing)} load(s) not in the {title}:')
            for l in missing:
                w(f'  SH - {l.shipment}  {l.status.name if l.status_id else "-":<11} {age(l)!s:>3} days  {(l.driver.full_name if l.driver_id else "-")[:24]:<24} driver pay {money(l.driver_pay)}  carrier pay {money(l.carrier_pay)}')
