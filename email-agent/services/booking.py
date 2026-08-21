"""
Creating the load in the TMS once a rate confirmation has been verified.

Reached only from the verification path in inbound.py — nothing else calls
this, so a load can never be created from an unverified document.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import models
from services import events, suggestions, telegram
from services.boxtruck import BoxTruckError, boxtruck

logger = logging.getLogger(__name__)


def _stop_payload(stop: dict, is_pickup: bool) -> dict:
    return {
        "address": stop.get("address"),
        "city": stop.get("city"),
        "state": stop.get("state"),
        "zip_code": stop.get("zip_code"),
        "driver_instructions": stop.get("driver_instructions"),
        "note": stop.get("facility"),
        "load_pickup": is_pickup,
        "load_drop": not is_pickup,
    }


def _first_datetime(stops: list[dict]) -> Optional[str]:
    for stop in stops:
        value = stop.get("date_time")
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return value
    return None


def _last_datetime(stops: list[dict]) -> Optional[str]:
    for stop in reversed(stops):
        value = stop.get("date_time")
        if isinstance(value, list):
            value = value[-1] if value else None
        if value:
            return value
    return None


async def book_verified_load(
    session: Session,
    negotiation: models.Negotiation,
    parsed: dict,
    ratecon: tuple[str, bytes],
) -> None:
    if negotiation.tms_load_id:
        logger.info(f"negotiation {negotiation.id} already booked as load {negotiation.tms_load_id}")
        return

    pickups = parsed.get("pickup_addresses") or []
    deliveries = parsed.get("delivery_locations") or []
    stops = [_stop_payload(s, True) for s in pickups] + [
        _stop_payload(s, False) for s in deliveries
    ]

    payload = {
        "company_id": negotiation.company_id,
        "driver_id": negotiation.driver_id,
        "broker_id": negotiation.tms_broker_id,
        "booked_by_id": negotiation.dispatcher_user_id,
        "load_number": parsed.get("load_number"),
        "carrier_pay": negotiation.effective_amount(),
        "driver_pay": negotiation.driver_amount,
        "pickup_date": _first_datetime(pickups),
        "drop_date": _last_datetime(deliveries),
        "note": parsed.get("special_instructions"),
        "dispatcher_note": (
            f"Booked automatically from a verified rate confirmation "
            f"(negotiation {negotiation.id})."
        ),
        "stops": stops,
    }

    try:
        result = await boxtruck.book_load(payload, ratecon=ratecon)
    except BoxTruckError as e:
        negotiation.status = models.FAILED
        negotiation.failure_reason = str(e)
        session.flush()
        logger.error(f"negotiation {negotiation.id}: booking failed — {e}")

        events.publish(
            events.BOOKING_FAILED,
            company_id=negotiation.company_id,
            negotiation_id=negotiation.id,
            load_uuid=negotiation.load_uuid,
            error=str(e),
            broker_email=negotiation.broker_email,
        )
        return

    negotiation.tms_load_id = result.get("load_id")
    negotiation.status = models.BOOKED
    # The load is booked; nothing still drafted for this thread needs sending.
    suggestions.supersede_pending(session, negotiation.id, suggestions.LOAD_BOOKED)
    session.flush()

    logger.info(
        f"negotiation {negotiation.id}: booked TMS load {negotiation.tms_load_id} "
        f"at ${negotiation.effective_amount():,.2f}"
    )

    await notify_driver(negotiation, parsed)

    events.publish(
        events.LOAD_BOOKED,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        tms_load_id=negotiation.tms_load_id,
        load_number=result.get("load_number"),
        shipment=result.get("shipment"),
        carrier_pay=negotiation.effective_amount(),
        driver_pay=negotiation.driver_amount,
        already_existed=result.get("already_existed", False),
    )


async def notify_driver(negotiation: models.Negotiation, parsed: dict) -> None:
    """A failure to reach Telegram must not undo a booked load."""
    chat_id = negotiation.driver_telegram_group_id
    if not chat_id:
        logger.warning(
            f"negotiation {negotiation.id}: no telegram group for driver "
            f"{negotiation.driver_id} — skipping driver notification"
        )
        return

    load = negotiation.load_snapshot or {}
    pickups = parsed.get("pickup_addresses") or []
    deliveries = parsed.get("delivery_locations") or []

    def place(stop_list: list[dict], fallback: str) -> str:
        if not stop_list:
            return fallback
        stop = stop_list[0]
        city, state = stop.get("city"), stop.get("state")
        return f"{city}, {state}" if city and state else (city or fallback)

    text = telegram.format_booked_notice(
        driver_name=load.get("driver_name") or "Driver",
        pickup=place(pickups, load.get("pick_up_at", "N/A")),
        delivery=place(deliveries, load.get("deliver_to", "N/A")),
        pickup_date=_first_datetime(pickups) or str(load.get("pick_up_date", "N/A")),
        driver_amount=negotiation.driver_amount,
        load_number=parsed.get("load_number"),
    )

    ok = await telegram.send_message(chat_id, text)
    if not ok:
        logger.error(
            f"negotiation {negotiation.id}: booked load {negotiation.tms_load_id} "
            f"but could not notify driver group {chat_id}"
        )
