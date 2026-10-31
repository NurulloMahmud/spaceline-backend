"""The shapes the Accounting page reads. Built by hand from select_related/prefetched rows so a
page of payouts or invoices costs a fixed number of queries."""
from .models import Invoice, Payout, ZERO, debt_of
from .services import QUICK_PAY_PERCENT, quick_pay_quote


def user_dict(u):
    if not u:
        return None
    return {'id': u.id, 'username': u.username, 'first_name': u.first_name, 'last_name': u.last_name}


def load_dict(load):
    return {
        'id': load.id,
        'shipment': f'SH - {load.shipment}' if load.shipment else None,
        'load_number': load.load_number,
        'pickup_date': load.pickup_date,
        'drop_date': load.drop_date,
        'driver_pay': load.driver_pay,
        'carrier_pay': load.carrier_pay,
        'payment_type': load.payment_type,
        'status': {'id': load.status_id, 'name': load.status.name} if load.status_id else None,
        'driver': {'id': load.driver_id, 'full_name': load.driver.full_name, 'unit_number': load.driver.unit_number} if load.driver_id else None,
        'broker': {'id': load.broker_id, 'name': load.broker.name, 'mc': load.broker.mc} if load.broker_id else None,
        'payout_id': load.payout.id if hasattr(load, 'payout') else None,
        'invoice_id': load.invoice.id if hasattr(load, 'invoice') else None,
        # The Loads page's files and delivered time, for the Factoring table's Files and
        # Delivered columns. The files come prefetched (see invoice_queryset) so a page of
        # invoices still costs a fixed number of queries.
        'delivered_at': load.delivered_at,
        'load_files': [
            {'id': f.id, 'name': f.name, 'file': f.file.url if f.file else f.rc_url}
            for f in load.loadfile_set.all()
        ],
    }


def deduction_dict(d):
    settled, held, owed = debt_of(d)
    return {
        'id': d.id, 'driver': {'id': d.driver_id, 'full_name': d.driver.full_name, 'unit_number': d.driver.unit_number},
        'type': {'id': d.type_id, 'name': d.type.name} if d.type_id else None,
        'amount': d.amount, 'fee': d.fee, 'date': d.date, 'note': d.note or '',
        'created_by': user_dict(d.created_by),
        'settled': settled, 'held': held, 'owed': owed,
        'state': 'settled' if owed <= 0 else 'settling' if held > 0 else 'open',
        'last_updated': d.last_updated,
    }


def link_dict(link, payout_status):
    d = link.deduction
    settled, held, owed = debt_of(d)
    return {
        'id': link.id, 'amount': link.amount, 'settled': payout_status == Payout.PAID,
        'deduction': {'id': d.id, 'type': d.type.name if d.type_id else 'Expense', 'amount': d.amount, 'fee': d.fee, 'date': d.date, 'note': d.note or '',
                      'settled': settled, 'held': held, 'owed': owed},
    }


def payout_dict(p, detail=False):
    links = list(p.expenses.all())
    applied = sum((l.amount for l in links), ZERO)
    out = {
        'id': p.id, 'load': load_dict(p.load), 'gross': p.gross, 'applied': applied, 'amount': max(ZERO, p.gross - applied),
        'quick_pay_fee': p.quick_pay_fee, 'payment_type': p.payment_type, 'status': p.status,
        'confirmed_at': p.confirmed_at, 'confirmed_by': user_dict(p.confirmed_by),
        'paid_at': p.paid_at, 'paid_by': user_dict(p.paid_by), 'reopened_at': p.reopened_at,
        'expenses': [link_dict(l, p.status) for l in links],
        'notes_count': len(p.notes.all()) if detail else getattr(p, 'notes_n', 0),
        'updated_at': p.updated_at, 'created_at': p.created_at,
    }
    if detail:
        out['quick_pay_percent'] = QUICK_PAY_PERCENT
        out['quick_pay_quote'] = quick_pay_quote(p.gross)
        out['notes'] = [{'id': n.id, 'user': user_dict(n.user), 'text': n.text, 'created_at': n.created_at} for n in p.notes.all()]
        out['history'] = [{'id': h.id, 'changed_by': user_dict(h.changed_by), 'description': h.description, 'created_at': h.created_at} for h in p.history.all()]
    return out


def invoice_dict(f, detail=False):
    out = {
        'id': f.id, 'load': load_dict(f.load), 'original': f.original, 'amount': f.amount, 'short': f.amount < f.original,
        'status': f.status, 'invoiced_at': f.invoiced_at, 'paid_at': f.paid_at,
        'notes_count': len(f.notes.all()) if detail else getattr(f, 'notes_n', 0),
        'updated_at': f.updated_at, 'created_at': f.created_at,
    }
    if detail:
        out['notes'] = [{'id': n.id, 'user': user_dict(n.user), 'text': n.text, 'created_at': n.created_at} for n in f.notes.all()]
        out['history'] = [{'id': h.id, 'changed_by': user_dict(h.changed_by), 'description': h.description, 'created_at': h.created_at} for h in f.history.all()]
    return out


def invoice_default_status(f):
    return Invoice.GOOD
