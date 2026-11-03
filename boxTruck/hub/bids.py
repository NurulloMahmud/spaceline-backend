"""Relay what a dispatcher did with a driver-app bid back to the hub.

A driver bid placed from the app lands in the loads service as
``action = driver_bid`` with ``hub target #N`` in its note. When a dispatcher
then places, declines, wins or loses a bid on that load, the hub target N is
told, and the hub notifies the driver.
"""
import logging
import re

import requests
from django.conf import settings

from users.models import Company

from .services import driver_hub

logger = logging.getLogger(__name__)

HUB_TARGET_RE = re.compile(r'hub target #(\d+)')


def hub_target_id(note):
    match = HUB_TARGET_RE.search(note or '')
    return int(match.group(1)) if match else None


def load_bids(load_id):
    """Every bid on one load from the loads service, read with the internal secret."""
    url = f"{settings.ATREK_BASE_URL.rstrip('/')}/loads/{load_id}/bids"
    try:
        resp = requests.get(
            url,
            params={'company_id': Company.current().id},
            headers={'X-Internal-Secret': settings.INTERNAL_SERVICE_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning('could not read bids for load %s from the loads service: %s', load_id, exc)
        return []
    data = body.get('data') if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = data.get('bids')
    return data if isinstance(data, list) else []


def driver_bid_targets(load_id, driver_id=None):
    """(hub target id, driver id) for each app bid on the load, optionally one driver's."""
    out = []
    for bid in load_bids(load_id):
        if bid.get('action') != 'driver_bid':
            continue
        target = hub_target_id(bid.get('note'))
        if not target:
            continue
        bid_driver = (bid.get('driver') or {}).get('id')
        if driver_id is not None and bid_driver is not None and int(bid_driver) != int(driver_id):
            continue
        out.append((target, bid_driver))
    return out


def relay_bid_status(load_id, status, carrier_amount=None, driver_amount=None, placed_by=None,
                     tenant_bid_id=None, note=None, driver_id=None, target_ids=None):
    """Tell the hub about every app bid on the load. Returns the target ids it reached."""
    if not driver_hub.configured():
        return []
    targets = list(target_ids or [])
    if not targets:
        targets = [t for t, _ in driver_bid_targets(load_id, driver_id)]
    reached = []
    for target in targets:
        try:
            driver_hub.update_bid_target(
                target, status,
                carrier_amount=carrier_amount, driver_amount=driver_amount,
                placed_by=placed_by, tenant_bid_id=tenant_bid_id, note=note,
            )
        except (driver_hub.DriverHubError, ValueError) as exc:
            logger.warning('hub bid target %s not updated to %s: %s', target, status, exc)
            continue
        reached.append(target)
    return reached
