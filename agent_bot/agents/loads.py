import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.ai import ai
from services.agent import agent
from services.boxmanage import boxtruck
from services.emailagent import emailagent
from services.matching import matches_driver
from database.repository import DriverPreferenceRepo, PendingOfferRepo

logger = logging.getLogger(__name__)


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


ZIP_SUFFIX = re.compile(r"\s+\d{5}(-\d{4})?$")


def format_place(place: Optional[str]) -> str:
    """'Chicago, IL 60609' -> 'Chicago, IL'."""
    if not place:
        return ""
    return ZIP_SUFFIX.sub("", place.strip())


def format_when(value, place: Optional[str] = None) -> str:
    """
    Feed timestamps arrive as '2026-07-23T15:00:00-04:00'. Drivers read this,
    so show local wall-clock time and where it applies. The offset is the
    stop's own local time, so it is displayed as-is, never converted.
    """
    if not value:
        return "N/A"

    if isinstance(value, datetime):
        stamp = value.strftime("%Y-%m-%d %H:%M")
    else:
        try:
            stamp = datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return str(value)

    where = format_place(place)
    return f"{stamp} ({where})" if where else stamp


def format_dims(load: dict) -> Optional[str]:
    dims = load.get("dims")
    if not dims or len(dims) != 3 or all(not d for d in dims):
        return None
    length, width, height = dims
    return f'{length}" × {width}" × {height}"'


def format_load_offer(load: dict, driver: dict, dead_head: float) -> str:
    full_name = driver.get("full_name", "Driver")
    dims_line = format_dims(load)
    return (
        f"👤 *{full_name}*\n\n"
        f"🚛 *New Load Match*\n\n"
        f"📍 Pick-up: {format_place(load.get('pick_up_at')) or 'N/A'}\n"
        f"📍 Delivery: {format_place(load.get('deliver_to')) or 'N/A'}\n"
        f"📅 Pick-up: {format_when(load.get('pick_up_date'), load.get('pick_up_at'))}\n"
        f"📅 Delivery: {format_when(load.get('delivery_date'), load.get('deliver_to'))}\n"
        f"🚗 Vehicle: {load.get('suggested_truck') or load.get('vehicle_type', 'N/A')}\n"
        f"📏 Miles: {load.get('miles', 'N/A')}\n"
        + (f"📐 Dimensions: {dims_line}\n" if dims_line else "")
        + f"📦 Pieces: {load.get('pieces', 'N/A')} | Weight: {load.get('weight', 'N/A')} lbs\n"
        f"🏁 Dead head: {dead_head:.1f} miles\n"
        # f"🏢 Broker: {load.get('contact_name', 'N/A')}\n\n"
        f"💰 Interested? *Reply to this message* with your rate (e.g. $1200)"
    )


DETAIL_FIELDS = ("dims", "pieces", "weight", "notes")


class LoadAgent:
    def __init__(self, pref_repo: DriverPreferenceRepo, offer_repo: PendingOfferRepo):
        self.pref_repo  = pref_repo
        self.offer_repo = offer_repo

    async def enrich_load(self, load: dict, load_uuid: str) -> dict:
        """The SSE feed omits dimensions; the load detail endpoint carries them."""
        if not load_uuid:
            return load

        detail = await agent.get_load(load_uuid)
        if not detail:
            logger.warning(f"Load {load_uuid}: no detail available, sending without dims")
            return load

        for field in DETAIL_FIELDS:
            if not load.get(field) and detail.get(field):
                load[field] = detail[field]
        return load

    async def on_new_load(self, load: dict, bot, load_uuid: Optional[str] = None) -> None:
        """
        `load` is the raw feed payload, whose `id` is the source's own numeric
        id. `load_uuid` is the loads service's identifier for the same load and
        is the only one its detail and bid endpoints accept.
        """
        if not load_uuid:
            logger.error(
                f"Load {load.get('id')} arrived without a service id — "
                f"dimensions and bids will not work for it"
            )
        pick_up_zip = load.get("pick_up_zip")
        pick_up_lat = load.get("pick_up_latitude")
        pick_up_lng = load.get("pick_up_longitude")

        if not pick_up_zip or not pick_up_lat or not pick_up_lng:
            logger.warning(f"Load {load.get('id')} missing pickup location")
            return

        nearby = await boxtruck.get_nearby_drivers(zip_code=pick_up_zip, radius=50)
        logger.info(f"Load {load.get('id')} nearby drivers count: {len(nearby)}")

        if not nearby:
            logger.info(f"No nearby drivers for load {load.get('id')}")
            return

        load = await self.enrich_load(load, load_uuid)

        # A driver is left alone while their bid is with a broker, and while
        # they are actually running a load, so one truck is never worked onto
        # two jobs at once.
        negotiating = await emailagent.held_driver_ids()
        on_a_load = await boxtruck.busy_driver_ids()
        held = negotiating | on_a_load
        if held:
            logger.info(
                f"Load {load.get('id')}: skipping {len(held)} driver(s) — "
                f"{len(negotiating)} mid-negotiation, {len(on_a_load)} already on a load"
            )

        sent_count = 0
        for driver in nearby:
            telegram_group_id = driver.get("telegram_group_id")
            driver_id  = driver["id"]
            driver_lat = driver.get("current_latitude") or driver.get("current_lat")
            driver_lng = driver.get("current_longitude") or driver.get("current_lng")
            vehicle    = driver.get("vehicle")

            logger.info(f"Checking driver {driver_id}: telegram={telegram_group_id} lat={driver_lat} lng={driver_lng} vehicle={vehicle}")

            if driver_id in held:
                why = "already running a load" if driver_id in on_a_load else "bid is with a broker"
                logger.info(f"Driver {driver_id} skipped: {why}")
                continue

            if not telegram_group_id:
                logger.info(f"Driver {driver_id} skipped: no telegram_group_id")
                continue

            if not driver_lat or not driver_lng:
                logger.info(f"Driver {driver_id} skipped: no location")
                continue

            if not vehicle:
                logger.info(f"Driver {driver_id} skipped: no vehicle")
                continue

            dead_head = haversine(
                float(driver_lat), float(driver_lng),
                float(pick_up_lat), float(pick_up_lng),
            )

            driver_pref = self.pref_repo.get_effective(driver_id)
            ok, reason  = matches_driver(load, driver, driver_pref, dead_head)
            logger.info(f"Driver {driver_id} match={ok} reason={reason} dead_head={dead_head:.1f}mi")

            if not ok:
                continue

            offer_load_id = load_uuid or str(load.get("id"))

            existing = self.offer_repo.get_active(driver_id)
            if existing and existing.load_id == offer_load_id:
                continue

            try:
                sent = await bot.send_message(
                    chat_id=telegram_group_id,
                    text=format_load_offer(load, driver, dead_head),
                    parse_mode="Markdown",
                )

                self.offer_repo.create(
                    driver_id=driver_id,
                    load_id=offer_load_id,
                    load_snapshot=load,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
                    telegram_message_id=getattr(sent, "message_id", None),
                )

                sent_count += 1
                logger.info(f"Sent load {load.get('id')} to driver {driver_id} ({dead_head:.1f}mi)")
            except Exception as e:
                logger.error(f"Failed to send to driver {driver_id}: {e}")
        logger.info(f"Load {load.get('id')} sent to {sent_count} drivers")

    async def handle_offer_reply(
        self, message: str, driver: dict, offer
    ) -> Optional[str]:
        """
        Handle a driver replying to a specific load offer.

        Only a stated price does anything. Anything else gets silence — the
        driver's group is theirs to talk in, and guessing at intent used to
        cancel live offers over ordinary chatter.
        """
        driver_id = driver["id"]

        reply = ai.classify_offer_reply(message)
        if not reply:
            logger.warning(
                f"Driver {driver_id}: could not classify reply to offer {offer.id}, ignoring"
            )
            return None

        intent = reply.get("intent")
        amount = reply.get("amount")

        if intent == "interested":
            return "👍 What rate do you want for this load? (e.g. $1200)"

        if intent != "bid" or not amount:
            logger.info(f"Driver {driver_id}: reply to offer {offer.id} was '{intent}', ignoring")
            return None

        company_id = (driver.get("company") or {}).get("id")
        if not company_id:
            logger.error(
                f"Driver {driver_id} has no company — cannot record bid for load {offer.load_id}"
            )
            return (
                "⚠️ Got your rate, but your profile is missing a company.\n"
                "Please contact your dispatcher."
            )

        recorded = await agent.place_bid(
            load_id=offer.load_id,
            company_id=company_id,
            driver_id=driver_id,
            amount=float(amount),
            driver_amount=float(amount),
            note=message,
        )
        if not recorded:
            logger.error(
                f"Failed to record bid for driver {driver_id} on load {offer.load_id}"
            )
            return (
                "⚠️ Got your rate, but we couldn't reach dispatch just now.\n"
                "Please message your dispatcher directly."
            )

        self.offer_repo.update_status(offer.id, "accepted")
        return (
            f"✅ Got your rate: *${amount:,.0f}*\n"
            f"Your dispatcher will review and confirm shortly."
        )


    async def handle_load_inquiry(self, message: str, driver: dict) -> Optional[str]:
        is_load_inquiry = ai.is_load_inquiry(message)
        if not is_load_inquiry:
            return None

        return (
            "👀 Got it! We're checking available loads matching your location and preferences.\n"
            "You'll be notified as soon as a matching load comes in."
        )
