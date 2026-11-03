"""Push a payroll statement to the hub as a payout the driver sees in the app."""
import logging
from decimal import Decimal

from django.utils import timezone

from .services import driver_hub

logger = logging.getLogger(__name__)


def payout_status(statement):
    name = (statement.status.name if statement.status else '').lower()
    if name == 'closed':
        return 'paid'
    if name == 'approved' or statement.final:
        return 'scheduled'
    return None


def payout_payload(statement, status):
    gross = statement.gross_amount or Decimal('0.00')
    lines = []
    load_count = 0
    for sl in statement.statementload_set.select_related('load').order_by('id'):
        load = sl.load
        if not load:
            continue
        load_count += 1
        lines.append({
            'kind': 'load',
            'label': load.name or load.load_number or (f'SH-{load.shipment}' if load.shipment else 'Load'),
            'reference': load.load_number or '',
            'amount': float(load.driver_pay or 0),
            'date': load.pickup_date.date().isoformat() if load.pickup_date else None,
        })
    deductions = Decimal('0.00')
    for sd in statement.statementdeduction_set.select_related('deduction', 'deduction__type').order_by('id'):
        d = sd.deduction
        amount = (d.amount or Decimal('0.00')) + (d.fee or Decimal('0.00'))
        deductions += amount
        lines.append({
            'kind': 'deduction',
            'label': (d.type.name if d.type else None) or d.note or 'Deduction',
            'reference': '',
            'amount': float(-amount),
            'date': d.date.isoformat() if d.date else None,
        })
    net = gross - deductions
    paid_at = timezone.now() if status == 'paid' else None
    return {
        'external_id': f'statement-{statement.id}',
        'hub_driver_id': statement.driver.hub_driver_id,
        'period_start': statement.start_date,
        'period_end': statement.end_date,
        'load_count': load_count,
        'gross': gross,
        'deductions': deductions,
        'net': net,
        'status': status,
        'scheduled_for': None,
        'paid_at': paid_at,
        'lines': lines,
    }


def sync_statement_payout(statement):
    """Best effort: a statement update must never fail because the hub is down."""
    driver = statement.driver
    if not driver or not driver.hub_driver_id or not driver_hub.configured():
        return None
    status = payout_status(statement)
    if not status:
        return None
    try:
        return driver_hub.upsert_payout(**payout_payload(statement, status))
    except driver_hub.DriverHubError as exc:
        logger.warning('payout for statement %s not sent to the hub: %s', statement.id, exc)
        return None
