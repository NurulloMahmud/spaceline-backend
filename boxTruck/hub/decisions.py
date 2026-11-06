"""A dispatcher's decisions on driver-app bids, kept on our side (see DriverBidDecision)."""

import logging
from typing import Any

from django.utils import timezone

from hiring.models import Driver

from .bids import AppBid, driver_bid_targets
from .models import DriverBidDecision

logger = logging.getLogger(__name__)


def record_declines(
    load_id: str,
    app_bids: list[AppBid],
    note: Any = None,
    user: Any = None,
    driver_id: Any = None,
) -> list[DriverBidDecision]:
    """Create or update a declined row for each of these bids. Returns the rows.

    Bids named only by an old hub target id get their bid id from the load.
    """
    bids = [b for b in app_bids if b.bid_id]
    targets_only = {b.target_id for b in app_bids if not b.bid_id and b.target_id}
    if targets_only:
        bids += [
            b
            for b in driver_bid_targets(load_id, driver_id)
            if b.target_id in targets_only and b.bid_id
        ]
    driver_ids = {_int(b.driver_id) for b in bids} - {None}
    known = set(Driver.objects.filter(pk__in=driver_ids).values_list("pk", flat=True))
    decider = user if getattr(user, "is_authenticated", False) else None
    rows = []
    for bid in {b.bid_id: b for b in bids}.values():
        driver = _int(bid.driver_id)
        row, created = DriverBidDecision.objects.update_or_create(
            bid_id=bid.bid_id,
            defaults={
                "load_id": str(load_id),
                "driver_id": driver if driver in known else None,
                "status": DriverBidDecision.DECLINED,
                "note": str(note or "").strip(),
                "decided_by": decider,
                "decided_at": timezone.now(),
            },
        )
        logger.info(
            "driver bid %s on load %s declined by %s%s",
            bid.bid_id,
            load_id,
            getattr(decider, "username", "a service"),
            "" if created else " (again)",
        )
        rows.append(row)
    return rows


def decision_dict(row: DriverBidDecision) -> dict[str, Any]:
    who = row.decided_by
    return {
        "bid_id": row.bid_id,
        "load_id": row.load_id,
        "driver_id": row.driver_id,
        "status": row.status,
        "note": row.note,
        "decided_by": (
            {
                "id": who.id,
                "name": f"{who.first_name} {who.last_name}".strip() or who.username,
            }
            if who
            else None
        ),
        "decided_at": row.decided_at,
    }


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
