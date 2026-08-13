"""
Background upkeep.

A bid the broker never answered would otherwise sit open forever: the load
stays unavailable to bid again (`already_open`), and the driver stays held off
new offers. It is closed automatically once the wait is clearly over.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from config.settings import config
from database import models
from database.connection import get_session
from services import events

logger = logging.getLogger(__name__)


def close_stale_bids(session: Session, now: datetime | None = None) -> list[models.Negotiation]:
    """
    Close negotiations still in `bid_sent` past the wait.

    Only `bid_sent` is swept: once a broker has replied the thread is a live
    conversation, and a dispatcher decides when that ends.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=config.STALE_BID_MINUTES)

    stale = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.status == models.BID_SENT,
            models.Negotiation.created_at < cutoff,
        )
        .all()
    )

    for negotiation in stale:
        negotiation.status = models.CLOSED
        negotiation.failure_reason = (
            f"Closed automatically: the broker did not reply within "
            f"{config.STALE_BID_MINUTES} minutes."
        )
        logger.info(
            f"negotiation {negotiation.id}: closed automatically, no broker reply "
            f"in {config.STALE_BID_MINUTES} minutes"
        )
        events.publish(
            events.NEGOTIATION_UPDATED,
            company_id=negotiation.company_id,
            negotiation_id=negotiation.id,
            load_uuid=negotiation.load_uuid,
            status=models.CLOSED,
            auto_closed=True,
            reason=negotiation.failure_reason,
            broker_email=negotiation.broker_email,
        )

    return stale


async def sweep_forever() -> None:
    """Runs for the life of the process. A failed pass never stops the loop."""
    logger.info(
        f"stale-bid sweeper running every {config.SWEEP_INTERVAL_SECONDS}s, "
        f"closing unanswered bids after {config.STALE_BID_MINUTES}m"
    )
    while True:
        try:
            await asyncio.sleep(config.SWEEP_INTERVAL_SECONDS)
            with get_session() as session:
                closed = close_stale_bids(session)
                if closed:
                    session.commit()
        except asyncio.CancelledError:
            logger.info("stale-bid sweeper stopping")
            raise
        except Exception:
            logger.exception("stale-bid sweep failed; continuing")


def held_driver_ids(session: Session, now: datetime | None = None) -> list[int]:
    """
    Drivers whose bid is live with a broker right now.

    The telegram bot skips these when matching a load, so a driver who has just
    been bid is not offered something else while we wait on the broker.

    An unanswered bid holds only for DRIVER_HOLD_MINUTES. A negotiation the
    broker has actually engaged with holds for as long as it stays open, with
    no timer: while dispatch is talking terms for this driver, offering them
    other freight is how you end up double-booking one truck.

    Drivers already running a load are held too, but that is the TMS's answer,
    not this service's — the bot unions this list with the busy drivers from
    boxTruck.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(minutes=config.DRIVER_HOLD_MINUTES)

    rows = (
        session.query(models.Negotiation.driver_id)
        .filter(
            models.Negotiation.driver_id.isnot(None),
            or_(
                # Waiting on a broker who has not answered yet: a short hold,
                # and the sweeper closes the bid outright at STALE_BID_MINUTES.
                and_(
                    models.Negotiation.status == models.BID_SENT,
                    models.Negotiation.created_at >= since,
                ),
                # A live conversation, or a rate confirmation being checked.
                # Someone is actively working this driver onto this load, so
                # the hold lasts as long as that does — no time cap.
                models.Negotiation.status.in_(
                    [models.NEGOTIATING, models.RATECON_RECEIVED]
                ),
            ),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]
