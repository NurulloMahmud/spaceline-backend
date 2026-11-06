"""Relay what a dispatcher did with a driver-app bid back to the hub.

A driver bid placed from the app lands in the loads service as
``action = driver_bid``, and the hub keeps that bid's id. When a dispatcher
then places, declines, wins or loses a bid on that load, each app bid is told
by that id, and the hub notifies the driver. The note is the driver's own
text; a few old bids carry ``hub target #N`` in it, and those are told by the
target id as before.
"""

import logging
import re
from typing import Any, NamedTuple

import requests
from django.conf import settings

from users.models import Company

from .services import driver_hub

logger = logging.getLogger(__name__)

HUB_TARGET_RE = re.compile(r"hub target #(\d+)")


class AppBid(NamedTuple):
    """One driver bid on a load: by hub target id (old tagged bids) or by its loads-service id."""

    target_id: int | None
    tenant_bid_id: str | None
    driver_id: Any = None


def hub_target_id(note):
    match = HUB_TARGET_RE.search(note or "")
    return int(match.group(1)) if match else None


def load_bids(load_id):
    """Every bid on one load from the loads service, read with the internal secret."""
    url = f"{settings.ATREK_BASE_URL.rstrip('/')}/loads/{load_id}/bids"
    try:
        resp = requests.get(
            url,
            params={"company_id": Company.current().id},
            headers={"X-Internal-Secret": settings.INTERNAL_SERVICE_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "could not read bids for load %s from the loads service: %s", load_id, exc
        )
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = data.get("bids")
    return data if isinstance(data, list) else []


def driver_bid_targets(load_id: str, driver_id: Any = None) -> list[AppBid]:
    """Each driver bid on the load, optionally only one driver's.

    Telegram driver bids are in the list too; the hub answers 404 for those.
    """
    out = []
    for bid in load_bids(load_id):
        if bid.get("action") != "driver_bid":
            continue
        bid_driver = (bid.get("driver") or {}).get("id")
        if (
            driver_id is not None
            and bid_driver is not None
            and int(bid_driver) != int(driver_id)
        ):
            continue
        target = hub_target_id(bid.get("note"))
        bid_id = str(bid.get("id") or "").strip() or None
        if target:
            out.append(AppBid(target, None, bid_driver))
        elif bid_id:
            out.append(AppBid(None, bid_id, bid_driver))
    return out


def relay_bid_status(
    load_id: str,
    status: str,
    carrier_amount: Any = None,
    driver_amount: Any = None,
    placed_by: Any = None,
    tenant_bid_id: Any = None,
    note: Any = None,
    driver_id: Any = None,
    target_ids: list[int] | None = None,
    driver_bid_ids: list[str] | None = None,
) -> list[int | str]:
    """Tell the hub about every app bid on the load.

    `target_ids` (hub target ids) and `driver_bid_ids` (loads-service ids of
    driver bids, taken as `driver_id`'s) name the bids outright; otherwise
    they are read from the load. `tenant_bid_id` is the dispatcher's own bid, sent with the old
    by-target call only. Returns the hub target ids reached (the driver bid id
    when the hub did not say which target).
    """
    if not driver_hub.configured():
        return []
    bids = [AppBid(int(t), None) for t in target_ids or []]
    bids += [
        AppBid(None, str(b), driver_id) for b in driver_bid_ids or [] if str(b).strip()
    ]
    if not bids:
        bids = driver_bid_targets(load_id, driver_id)
    amounts = {
        "carrier_amount": carrier_amount,
        "driver_amount": driver_amount,
        "placed_by": placed_by,
        "note": note,
    }
    reached: list[int | str] = []
    for bid in bids:
        name = bid.target_id or bid.tenant_bid_id
        try:
            if bid.target_id:
                driver_hub.update_bid_target(
                    bid.target_id, status, tenant_bid_id=tenant_bid_id, **amounts
                )
                reached.append(bid.target_id)
                continue
            # The load and driver let the hub find the bid if it never got its id.
            reply = driver_hub.update_bid_by_tenant_bid(
                bid.tenant_bid_id,
                status,
                load_id=load_id,
                tenant_driver_id=bid.driver_id,
                **amounts,
            )
        except driver_hub.DriverHubError as exc:
            if exc.status_code == 404:
                # Not an app bid (Telegram) or one the hub no longer has: nothing to retry.
                logger.info("no driver-app bid %s on the hub for %s", name, status)
            else:
                logger.warning("hub bid %s not updated to %s: %s", name, status, exc)
            continue
        except ValueError as exc:
            logger.warning("hub bid %s not updated to %s: %s", name, status, exc)
            continue
        target = reply.get("id") if isinstance(reply, dict) else None
        reached.append(target if isinstance(target, int) else bid.tenant_bid_id)
    return reached
