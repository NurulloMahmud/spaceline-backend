"""HTTP client for the Driver Hub (btm-driver-app), tenant side.

Every call goes to ``{DRIVER_HUB_URL}/t/v1/...`` with the deployment's service
token as a bearer token. Callers that must not fail because the hub is down
(bid relays, payouts, blacklist pushes) catch DriverHubError and log it; the
invite endpoint lets it through so the dispatcher sees why nothing happened.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 10

BID_STATUSES = ('reviewing', 'placed', 'declined', 'won', 'lost', 'blacklisted')
PAYOUT_STATUSES = ('scheduled', 'paid')


class DriverHubError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"driver hub {status_code}: {detail}")


def configured():
    return bool(settings.DRIVER_HUB_URL and settings.DRIVER_HUB_TOKEN)


def _request(method, path, json=None):
    if not configured():
        raise DriverHubError(503, 'DRIVER_HUB_URL and DRIVER_HUB_TOKEN are not configured')
    url = f"{settings.DRIVER_HUB_URL.rstrip('/')}{path}"
    try:
        resp = requests.request(
            method,
            url,
            json=json,
            headers={'Authorization': f'Bearer {settings.DRIVER_HUB_TOKEN}'},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('driver hub %s %s unreachable: %s', method, path, exc)
        raise DriverHubError(502, 'driver hub unreachable') from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json().get('error', resp.text)
        except ValueError:
            detail = resp.text
        logger.warning('driver hub %s %s -> %s: %s', method, path, resp.status_code, detail)
        raise DriverHubError(resp.status_code, detail)

    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def invite(email, invited_by):
    """POST /t/v1/invites -> {invite_id, status, delivery, hub_driver_id}."""
    return _request('POST', '/t/v1/invites', json={'email': email, 'invited_by': invited_by})


def invite_status(invite_id):
    """GET /t/v1/invites/{id} -> {status: pending, accepted, declined}."""
    return _request('GET', f'/t/v1/invites/{int(invite_id)}')


def link_driver(hub_driver_id, tenant_driver_id):
    return _request('POST', f'/t/v1/drivers/{int(hub_driver_id)}/link', json={'tenant_driver_id': int(tenant_driver_id)})


def update_bid_target(target_id, status, carrier_amount=None, driver_amount=None,
                      placed_by=None, tenant_bid_id=None, note=None):
    """POST /t/v1/bid-targets/{id}/status. The hub notifies the driver on each change."""
    if status not in BID_STATUSES:
        raise ValueError(f'unknown bid target status {status!r}')
    body = {'status': status}
    if carrier_amount is not None:
        body['carrier_amount'] = float(carrier_amount)
    if driver_amount is not None:
        body['driver_amount'] = float(driver_amount)
    if placed_by:
        body['placed_by'] = str(placed_by)
    if tenant_bid_id:
        body['tenant_bid_id'] = str(tenant_bid_id)
    if note:
        body['note'] = str(note)
    return _request('POST', f'/t/v1/bid-targets/{int(target_id)}/status', json=body)


def upsert_payout(external_id, hub_driver_id, period_start, period_end, load_count,
                  gross, deductions, net, status, scheduled_for=None, paid_at=None, lines=None):
    """POST /t/v1/payouts, upsert by external_id."""
    if status not in PAYOUT_STATUSES:
        raise ValueError(f'unknown payout status {status!r}')
    body = {
        'external_id': str(external_id),
        'hub_driver_id': int(hub_driver_id),
        'period_start': _day(period_start),
        'period_end': _day(period_end),
        'load_count': int(load_count or 0),
        'gross': float(gross or 0),
        'deductions': float(deductions or 0),
        'net': float(net or 0),
        'status': status,
        'scheduled_for': _stamp(scheduled_for),
        'paid_at': _stamp(paid_at),
        'lines': lines or [],
    }
    return _request('POST', '/t/v1/payouts', json=body)


def replace_blacklist(brokers):
    """PUT /t/v1/blacklist with the full list: [{name, email, mc}]."""
    rows = [
        {
            'name': str(b.get('name') or ''),
            'email': str(b.get('email') or ''),
            'mc': str(b.get('mc') or ''),
        }
        for b in brokers
    ]
    return _request('PUT', '/t/v1/blacklist', json={'brokers': rows})


def _day(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _stamp(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)
