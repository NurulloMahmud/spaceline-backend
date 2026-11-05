"""Bring open payouts back in step with their load's payment type.

    manage.py sync_payment_types [--apply] [--take-fees]

Until payouts followed the load, a payment type changed on the Loads page after booking
never reached the payout. This lists every unpaid payout whose type disagrees with its
load and, with --apply, moves it (taking or returning the Quick Pay fee). A payout whose
type accounting changed by hand is left alone and listed separately. --take-fees also
takes the fee on open Quick Pay payouts that never had it.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from accounting.models import Payout
from accounting.services import log_payout, money, payment_type_of, set_payment_type, take_quick_pay_fee
from billing.models import Load


class Command(BaseCommand):
    help = "Align open payouts with their load's payment type"

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the changes; without it nothing is changed')
        parser.add_argument('--take-fees', action='store_true', help='Also take the fee on open Quick Pay payouts that never had it')

    def handle(self, *args, **opts):
        apply = opts['apply']
        moved = kept = fees = 0
        payouts = Payout.objects.exclude(status=Payout.PAID).select_related('load', 'load__driver').order_by('load_id')
        for p in payouts:
            want = payment_type_of(p.load)
            if want != p.payment_type:
                by_hand = p.history.filter(description__startswith='Payment type ').exclude(description__contains='changed on the load').exists()
                tag = f'load {p.load_id} (SH - {p.load.shipment}) payout {p.id}: {p.payment_type} -> {want}'
                if by_hand:
                    kept += 1
                    self.stdout.write(f'kept, set by hand in Accounting: {tag}')
                    continue
                moved += 1
                self.stdout.write(('moved: ' if apply else 'would move: ') + tag)
                if apply:
                    with transaction.atomic():
                        set_payment_type(p, want, None, 'synced from the load', write_load=False)
                        p.save(update_fields=['gross', 'quick_pay_fee', 'payment_type'])
                continue
            if p.payment_type == Payout.QUICK_PAY and p.quick_pay_fee <= 0 and p.gross > 0:
                fees += 1
                tag = f'load {p.load_id} (SH - {p.load.shipment}) payout {p.id}: Quick Pay without the fee on {money(p.gross)}'
                if apply and opts['take_fees']:
                    with transaction.atomic():
                        take_quick_pay_fee(p, None)
                        p.save(update_fields=['gross', 'quick_pay_fee'])
                    self.stdout.write(f'fee taken: {tag}')
                else:
                    self.stdout.write(f'fee not taken: {tag}')
        self.stdout.write(f'{moved} moved, {kept} kept, {fees} Quick Pay payouts without the fee' + ('' if apply else ' (dry run)'))
