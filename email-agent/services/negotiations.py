"""
Starting a negotiation: the dispatcher clicks "Bid" on the bid board, names a
price, and this sends the bid email to the broker and opens the thread.

This is the only email the system sends without a dispatcher approving the
exact text — because the dispatcher chose the number and the template is fixed.
"""
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from database import models
from services import bid_email, events
from services.atrek import atrek
from services.boxtruck import boxtruck
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)


class NegotiationError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


BROKER_EMAIL_FIELDS = (
    "contact_email",
    "email",
    "broker_email",
    "poster_email",
    "contact_e_mail",
)


def extract_broker_email(load_detail: dict) -> Optional[str]:
    """The posting contact's address, wherever the feed happens to put it."""
    for field in BROKER_EMAIL_FIELDS:
        value = load_detail.get(field)
        if isinstance(value, str) and "@" in value:
            return value.strip()

    contact = load_detail.get("contact")
    if isinstance(contact, dict):
        for field in BROKER_EMAIL_FIELDS:
            value = contact.get(field)
            if isinstance(value, str) and "@" in value:
                return value.strip()
    return None


RE_PREFIX = re.compile(r"^\s*(re\s*:\s*)+", re.IGNORECASE)


def reply_subject(negotiation) -> str:
    """
    The subject every reply on this negotiation must carry.

    Mail clients split a conversation into a new chain when the subject
    changes. The model used to name its own drafts, so a reply went out as
    "Re: Rate Confirmation Needed for Wood Dale to Dover" against an opening
    subject of "Bid — Wood Dale, IL to Dover, PA (Ref 2202907)" — Nylas kept
    its thread id, but the broker saw a brand new email. The thread's original
    subject is the only one we send.
    """
    base = RE_PREFIX.sub("", (negotiation.subject or "").strip())
    return f"Re: {base}" if base else "Re:"


def load_summary_text(load: dict) -> str:
    return "\n".join([
        f"Pick-up: {load.get('pick_up_at', 'N/A')} ({load.get('pick_up_zip', '')})",
        f"Delivery: {load.get('deliver_to', 'N/A')} ({load.get('deliver_zip', '')})",
        f"Pick-up date: {load.get('pick_up_date', 'N/A')}",
        f"Delivery date: {load.get('delivery_date', 'N/A')}",
        f"Miles: {load.get('miles', 'N/A')}",
        f"Vehicle: {load.get('suggested_truck') or load.get('vehicle_type', 'N/A')}",
        f"Pieces: {load.get('pieces', 'N/A')}, Weight: {load.get('weight', 'N/A')} lbs",
        f"Broker contact: {load.get('contact_name', 'N/A')}",
    ])


async def create_negotiation(
    session: Session,
    *,
    company_id: int,
    dispatcher_user_id: int,
    load_uuid: str,
    bid_amount: float,
    driver_bid_id: Optional[str] = None,
    driver_id: Optional[int] = None,
    driver_amount: Optional[float] = None,
    broker_email: Optional[str] = None,
    note: str = "",
) -> models.Negotiation:
    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == company_id)
        .first()
    )
    if not account:
        raise NegotiationError(
            "No dispatch mailbox is connected for this company. "
            "Connect one under Settings before bidding.",
            status_code=409,
            code="mailbox_not_connected",
        )

    existing = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.company_id == company_id,
            models.Negotiation.load_uuid == load_uuid,
            models.Negotiation.status.notin_([models.CLOSED, models.FAILED]),
        )
        .first()
    )
    if existing:
        raise NegotiationError(
            "A negotiation is already open on this load.",
            status_code=409,
            code="already_open",
        )

    load_detail = await atrek.get_load(load_uuid)
    if not load_detail:
        raise NegotiationError(
            "Could not load the freight details from the load board.",
            status_code=502,
            code="load_unavailable",
        )

    resolved_email = broker_email or extract_broker_email(load_detail)
    if not resolved_email:
        raise NegotiationError(
            "This load has no broker email address. Provide one to send the bid.",
            status_code=422,
            code="broker_email_required",
        )

    company = await boxtruck.get_company(company_id)
    if not company:
        raise NegotiationError(
            "Company profile not found in the TMS.",
            status_code=502,
            code="company_unavailable",
        )

    dispatcher = await boxtruck.get_dispatcher(dispatcher_user_id)
    driver = await boxtruck.get_driver(driver_id) if driver_id else None

    subject = bid_email.build_subject(load_detail)
    body = bid_email.build_html_body(
        bid_amount=bid_amount,
        load=load_detail,
        driver=driver,
        company=company,
        dispatcher=dispatcher,
    )
    plain_body = bid_email.build_body(
        bid_amount=bid_amount,
        load=load_detail,
        driver=driver,
        company=company,
        dispatcher=dispatcher,
    )

    negotiation = models.Negotiation(
        company_id=company_id,
        load_uuid=load_uuid,
        load_snapshot=load_detail,
        driver_bid_id=driver_bid_id,
        driver_id=driver_id,
        driver_amount=driver_amount,
        driver_telegram_group_id=(driver or {}).get("telegram_group_id"),
        dispatcher_user_id=dispatcher_user_id,
        bid_amount=bid_amount,
        broker_email=resolved_email,
        broker_name=load_detail.get("contact_name"),
        subject=subject,
        status=models.BID_SENT,
    )
    session.add(negotiation)
    session.flush()

    try:
        sent = await nylas.send_message(
            grant_id=account.nylas_grant_id,
            to_email=resolved_email,
            subject=subject,
            body=body,
        )
    except NylasError as e:
        logger.error(f"bid email failed for load {load_uuid}: {e}")
        raise NegotiationError(
            f"Could not send the bid email: {e}",
            status_code=502,
            code="send_failed",
        )

    negotiation.nylas_thread_id = sent.get("thread_id")
    session.add(
        models.EmailMessage(
            negotiation_id=negotiation.id,
            nylas_message_id=sent.get("id"),
            direction="outbound",
            from_email=account.email_address,
            to_email=resolved_email,
            subject=subject,
            body_text=plain_body,
            sent_by_user_id=dispatcher_user_id,
        )
    )
    session.flush()

    # Colour the load on the bid board as bid-on. A failure here must not undo
    # an email that is already gone.
    recorded = await atrek.record_dispatcher_bid(
        load_uuid=load_uuid,
        company_id=company_id,
        user_id=dispatcher_user_id,
        driver_id=driver_id,
        amount=bid_amount,
        driver_amount=driver_amount,
        note=note or "Bid emailed to broker",
    )
    if not recorded:
        logger.error(f"negotiation {negotiation.id}: failed to record dispatcher bid in atrek")

    events.publish(
        events.NEGOTIATION_UPDATED,
        company_id=company_id,
        negotiation_id=negotiation.id,
        load_uuid=load_uuid,
        status=models.BID_SENT,
        bid_amount=bid_amount,
        broker_email=resolved_email,
    )
    return negotiation
