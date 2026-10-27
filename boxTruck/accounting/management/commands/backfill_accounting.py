"""Create the payout and invoice for loads that were booked before the Accounting page existed.

    manage.py backfill_accounting --since 2026-08-24 [--dry-run]

Backfilled payouts do not take expenses (those were handled by the old statements), and
are marked paid when the load is already invoiced or factored or its pick-up is more than
--paid-after days ago; invoices follow the load status (Invoiced -> invoiced, Factored -> paid).
Every record says in its history that it was backfilled.
"""
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounting.models import Invoice, Payout
from accounting.services import create_for_load, log_invoice, log_payout
from billing.models import Load


class Command(BaseCommand):
    help = 'Create payouts and invoices for loads booked before the Accounting page'

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, help='Earliest pick-up date, YYYY-MM-DD')
        parser.add_argument('--paid-after', type=int, default=14, help='Payouts of loads picked up more than this many days ago start as paid')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        since = timezone.make_aware(datetime.strptime(opts['since'], '%Y-%m-%d'))
        cutoff = timezone.now() - timedelta(days=opts['paid_after'])
        loads = Load.objects.filter(pickup_date__gte=since).exclude(status__name__iexact='Rejected').select_related('status', 'driver').order_by('pickup_date')
        made = 0
        for load in loads:
            if Payout.objects.filter(load=load).exists() and Invoice.objects.filter(load=load).exists():
                continue
            if opts['dry_run']:
                self.stdout.write(f'would create for load {load.id} ({load.shipment})')
                made += 1
                continue
            with transaction.atomic():
                payout, invoice = create_for_load(load, take_expenses=False)
                name = (load.status.name if load.status else '').lower()
                if name in ('invoiced', 'factored', 'paid') or (load.pickup_date and load.pickup_date < cutoff):
                    payout.status = Payout.PAID
                    payout.confirmed_at = payout.paid_at = load.pickup_date or timezone.now()
                    payout.save()
                    log_payout(payout, None, 'Backfilled as paid from the load')
                else:
                    log_payout(payout, None, 'Backfilled from the load')
                if name == 'invoiced':
                    invoice.status = Invoice.INVOICED
                    invoice.invoiced_at = load.last_updated or timezone.now()
                    invoice.save()
                    log_invoice(invoice, None, 'Backfilled as invoiced from the load')
                elif name in ('factored', 'paid'):
                    invoice.status = Invoice.PAID
                    invoice.invoiced_at = invoice.paid_at = load.last_updated or timezone.now()
                    invoice.save()
                    log_invoice(invoice, None, 'Backfilled as paid from the load')
                else:
                    log_invoice(invoice, None, 'Backfilled from the load')
            made += 1
        self.stdout.write(self.style.SUCCESS(f'{made} load(s) {"would get" if opts["dry_run"] else "got"} accounting records'))
