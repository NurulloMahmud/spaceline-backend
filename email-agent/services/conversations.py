"""
Conversations: a negotiation seen as an email thread, with no stages.

Two things live here. What is new to a user: broker messages that arrived
after they last opened the conversation, tracked per user. And writing: the
dispatcher who sent the bid can email the broker on the thread at any time,
whatever the negotiation's status; everyone else can read it but not write.
"""
import html
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from database import models
from services import events
from services.negotiations import NegotiationError, reply_subject, thread_mailbox
from services.nylas_client import NylasError, nylas
from services.suggestions import REPLIED_IN_APP, supersede_pending

logger = logging.getLogger(__name__)

# Read tracking started here. A conversation someone never opened counts only
# broker messages after this as new, so the whole history did not turn unread
# on the day it shipped.
READS_SINCE = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)

SNIPPET_CHARS = 160


def _read_mark(user_id: int):
    """When `user_id` last opened the conversation of the EmailMessage row in
    scope, or READS_SINCE if they never did."""
    opened = (
        select(models.ConversationRead.read_at)
        .where(
            models.ConversationRead.negotiation_id == models.EmailMessage.negotiation_id,
            models.ConversationRead.user_id == user_id,
        )
        .correlate(models.EmailMessage)
        .scalar_subquery()
    )
    return func.coalesce(opened, READS_SINCE)


def new_to(user_id: int):
    """A broker message `user_id` has not opened yet. Messages the team sent
    are never new: the sender wrote them, and nobody else can."""
    return and_(
        models.EmailMessage.direction == "inbound",
        models.EmailMessage.created_at > _read_mark(user_id),
    )


def unread_counts(session: Session, user_id: int, negotiation_ids: list) -> dict:
    """negotiation id -> how many broker messages are new to `user_id`."""
    if not negotiation_ids:
        return {}
    rows = (
        session.query(models.EmailMessage.negotiation_id, func.count(models.EmailMessage.id))
        .filter(models.EmailMessage.negotiation_id.in_(negotiation_ids), new_to(user_id))
        .group_by(models.EmailMessage.negotiation_id)
        .all()
    )
    return {row[0]: row[1] for row in rows}


def has_unread(user_id: int):
    """For filtering negotiations: at least one broker message new to `user_id`."""
    return (
        select(models.EmailMessage.id)
        .where(models.EmailMessage.negotiation_id == models.Negotiation.id, new_to(user_id))
        .correlate(models.Negotiation)
        .exists()
    )


def last_activity():
    """When a conversation last had a message, or when it started."""
    latest = (
        select(func.max(models.EmailMessage.created_at))
        .where(models.EmailMessage.negotiation_id == models.Negotiation.id)
        .correlate(models.Negotiation)
        .scalar_subquery()
    )
    return func.coalesce(latest, models.Negotiation.created_at)


def last_messages(session: Session, negotiation_ids: list) -> dict:
    """negotiation id -> its latest message, for the preview line."""
    if not negotiation_ids:
        return {}
    rows = (
        session.query(models.EmailMessage)
        .filter(models.EmailMessage.negotiation_id.in_(negotiation_ids))
        .distinct(models.EmailMessage.negotiation_id)
        .order_by(models.EmailMessage.negotiation_id, models.EmailMessage.created_at.desc())
        .all()
    )
    return {m.negotiation_id: m for m in rows}


def snippet(message: Optional[models.EmailMessage]) -> Optional[dict]:
    if message is None:
        return None
    text = " ".join((message.body_text or "").split())
    return {
        "direction": message.direction,
        "from_email": message.from_email,
        "sent_by_user_id": message.sent_by_user_id,
        "text": text[:SNIPPET_CHARS],
        "has_attachments": message.has_attachments,
        "created_at": message.created_at,
    }


def get_for_company(session: Session, negotiation_id: str, company_id: int) -> models.Negotiation:
    negotiation = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.id == negotiation_id,
            models.Negotiation.company_id == company_id,
        )
        .first()
    )
    if not negotiation:
        raise NegotiationError("Conversation not found.", status_code=404, code="not_found")
    return negotiation


def mark_read(session: Session, negotiation: models.Negotiation, user_id: int) -> datetime:
    """Everything in the conversation up to now is no longer new to `user_id`."""
    now = datetime.now(timezone.utc)
    session.execute(
        insert(models.ConversationRead)
        .values(negotiation_id=negotiation.id, user_id=user_id, read_at=now)
        .on_conflict_do_update(
            index_elements=["negotiation_id", "user_id"],
            set_={"read_at": now},
        )
    )
    session.flush()
    return now


def _reply_targets(session: Session, negotiation: models.Negotiation) -> tuple[Optional[str], Optional[str]]:
    """(Nylas message id, RFC Message-ID) of the message a new email answers:
    the broker's latest, else ours. The Nylas id threads it inside the
    mailbox; the RFC id still threads it on the broker's side when the Nylas
    id belongs to a grant that has since been replaced."""
    messages = (
        session.query(models.EmailMessage)
        .filter(models.EmailMessage.negotiation_id == negotiation.id)
        .order_by(models.EmailMessage.created_at.desc())
        .all()
    )
    ordered = [m for m in messages if m.direction == "inbound"] + [
        m for m in messages if m.direction != "inbound"
    ]
    nylas_id = next((m.nylas_message_id for m in ordered if m.nylas_message_id), None)
    rfc_id = next((m.rfc_message_id for m in ordered if m.rfc_message_id), None)
    if rfc_id is None and not messages:
        rfc_id = (negotiation.load_snapshot or {}).get("email_message_id")
    return nylas_id, rfc_id


async def write(
    session: Session,
    negotiation: models.Negotiation,
    user_id: int,
    body: str,
) -> models.EmailMessage:
    """
    Email the broker on the conversation's thread, from the mailbox that sent
    the bid. Only the dispatcher who sent the bid may, and they may at any
    time: a closed or booked negotiation is still their conversation.
    """
    if negotiation.dispatcher_user_id is not None and negotiation.dispatcher_user_id != user_id:
        raise NegotiationError(
            "Only the dispatcher who sent this bid can write in this conversation.",
            status_code=403,
            code="not_your_conversation",
        )
    text = (body or "").strip()
    if not text:
        raise NegotiationError("There is nothing to send.", status_code=422, code="empty_body")

    account = thread_mailbox(session, negotiation)
    if not account:
        raise NegotiationError(
            "No dispatch mailbox is connected for this company.",
            status_code=409,
            code="mailbox_not_connected",
        )
    if account.status != "active":
        raise NegotiationError(
            f"{account.email_address} sent this bid and is no longer connected. Reconnect it to write.",
            status_code=409,
            code="mailbox_not_connected",
        )

    subject = reply_subject(negotiation)
    html_body = html.escape(text).replace("\n", "<br>")
    nylas_id, rfc_id = _reply_targets(session, negotiation)
    try:
        try:
            sent = await nylas.send_message(
                grant_id=account.nylas_grant_id,
                to_email=negotiation.broker_email,
                subject=subject,
                body=html_body,
                reply_to_message_id=nylas_id,
                in_reply_to=rfc_id,
            )
        except NylasError as e:
            if not nylas_id:
                raise
            # The message may live in a grant this mailbox no longer has (it
            # was reconnected). Thread on the Message-ID instead.
            logger.warning(
                f"conversation {negotiation.id}: reply_to {nylas_id} refused ({e}), "
                "sending on the Message-ID instead"
            )
            sent = await nylas.send_message(
                grant_id=account.nylas_grant_id,
                to_email=negotiation.broker_email,
                subject=subject,
                body=html_body,
                in_reply_to=rfc_id,
            )
    except NylasError as e:
        logger.error(f"conversation {negotiation.id}: send failed: {e}")
        raise NegotiationError(f"Could not send the email: {e}", status_code=502, code="send_failed")

    if not negotiation.nylas_thread_id and sent.get("thread_id"):
        negotiation.nylas_thread_id = sent["thread_id"]

    message = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=sent.get("id"),
        direction="outbound",
        from_email=account.email_address,
        to_email=negotiation.broker_email,
        subject=subject,
        body_text=text,
        sent_by_user_id=user_id,
    )
    session.add(message)

    # A conversation the broker never answered was closed by the sweeper; the
    # dispatcher writing again opens it back up.
    if negotiation.status in (models.BID_SENT, models.MISMATCH, models.CLOSED):
        negotiation.status = models.NEGOTIATING
    superseded = supersede_pending(session, negotiation.id, REPLIED_IN_APP)
    session.flush()

    events.publish(
        events.NEGOTIATION_UPDATED,
        company_id=negotiation.company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        message_id=str(message.id),
        status=negotiation.status,
        superseded=superseded,
    )
    return message
