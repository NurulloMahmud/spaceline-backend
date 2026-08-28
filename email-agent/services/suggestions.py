"""
Dispatcher decisions on drafted replies. A suggestion only reaches a broker
through send(), and only when a dispatcher calls it.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database import models
from services import events
from services.negotiations import NegotiationError, reply_subject
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)


def get_for_company(
    session: Session, suggestion_id: str, company_id: int
) -> models.Suggestion:
    suggestion = (
        session.query(models.Suggestion)
        .join(models.Negotiation)
        .filter(
            models.Suggestion.id == suggestion_id,
            models.Negotiation.company_id == company_id,
        )
        .first()
    )
    if not suggestion:
        raise NegotiationError("Suggestion not found.", status_code=404, code="not_found")
    return suggestion


def ignore(
    session: Session, suggestion_id: str, company_id: int, user_id: int
) -> models.Suggestion:
    suggestion = get_for_company(session, suggestion_id, company_id)
    if suggestion.status != models.PENDING:
        raise NegotiationError(
            f"This suggestion was already {suggestion.status}.",
            status_code=409,
            code="already_resolved",
        )

    suggestion.status = models.IGNORED
    suggestion.resolved_by_user_id = user_id
    suggestion.resolved_at = datetime.now(timezone.utc)
    session.flush()

    events.publish(
        events.NEGOTIATION_UPDATED,
        company_id=company_id,
        negotiation_id=suggestion.negotiation_id,
        suggestion_id=str(suggestion.id),
        suggestion_status=models.IGNORED,
    )
    return suggestion


async def send(
    session: Session,
    suggestion_id: str,
    company_id: int,
    user_id: int,
    body: Optional[str] = None,
    subject: Optional[str] = None,
) -> models.Suggestion:
    suggestion = get_for_company(session, suggestion_id, company_id)
    if suggestion.status != models.PENDING:
        raise NegotiationError(
            f"This suggestion was already {suggestion.status}.",
            status_code=409,
            code="already_resolved",
        )

    negotiation = suggestion.negotiation
    final_body = (body if body is not None else suggestion.draft_body) or ""
    # The subject is the thread's, never the model's and never the caller's:
    # changing it makes the broker's mail client show a brand new chain.
    final_subject = reply_subject(negotiation)

    if not final_body.strip():
        raise NegotiationError(
            "There is nothing to send — provide a body.",
            status_code=422,
            code="empty_body",
        )

    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == company_id)
        .first()
    )
    if not account:
        raise NegotiationError(
            "No dispatch mailbox is connected for this company.",
            status_code=409,
            code="mailbox_not_connected",
        )

    reply_to = None
    if suggestion.in_reply_to_message_id:
        source = (
            session.query(models.EmailMessage)
            .filter(models.EmailMessage.id == suggestion.in_reply_to_message_id)
            .first()
        )
        reply_to = source.nylas_message_id if source else None

    try:
        sent = await nylas.send_message(
            grant_id=account.nylas_grant_id,
            to_email=negotiation.broker_email,
            subject=final_subject,
            body=final_body.replace("\n", "<br>"),
            reply_to_message_id=reply_to,
        )
    except NylasError as e:
        logger.error(f"suggestion {suggestion_id} send failed: {e}")
        raise NegotiationError(
            f"Could not send the email: {e}", status_code=502, code="send_failed"
        )

    # The thread id is captured on the send that opened the negotiation, but a
    # provider does not always return one there. Every later send is another
    # chance to learn it, and without it the broker's replies match only by
    # subject.
    if not negotiation.nylas_thread_id and sent.get("thread_id"):
        negotiation.nylas_thread_id = sent["thread_id"]

    session.add(
        models.EmailMessage(
            negotiation_id=negotiation.id,
            nylas_message_id=sent.get("id"),
            direction="outbound",
            from_email=account.email_address,
            to_email=negotiation.broker_email,
            subject=final_subject,
            body_text=final_body,
            sent_by_user_id=user_id,
        )
    )

    edited = body is not None and body.strip() != (suggestion.draft_body or "").strip()
    suggestion.status = models.EDITED_SENT if edited else models.SENT
    suggestion.final_body = final_body
    suggestion.resolved_by_user_id = user_id
    suggestion.resolved_at = datetime.now(timezone.utc)

    if negotiation.status in (models.BID_SENT, models.MISMATCH):
        negotiation.status = models.NEGOTIATING

    # Sending one reply answers the broker; any other draft on this thread was
    # written for a message we have now responded to.
    superseded = supersede_pending(
        session, negotiation.id, REPLIED_IN_APP, keep_id=suggestion.id
    )
    session.flush()

    events.publish(
        events.NEGOTIATION_UPDATED,
        company_id=company_id,
        negotiation_id=negotiation.id,
        load_uuid=negotiation.load_uuid,
        suggestion_id=str(suggestion.id),
        suggestion_status=suggestion.status,
        status=negotiation.status,
        superseded=superseded,
    )
    return suggestion


# --- drafts the conversation moved past --------------------------------------

REPLIED_ELSEWHERE = (
    "Not needed — this conversation was answered from a mail client "
    "before the draft was sent."
)
REPLIED_IN_APP = "Not needed — a different reply was sent on this conversation."
NEWER_DRAFT = "Not needed — a newer reply was drafted for this conversation."
ANSWERED_ALREADY = (
    "Not needed — this conversation was answered after the draft was written."
)
NEGOTIATION_CLOSED = "Not needed — the bid was closed before the draft was sent."
LOAD_BOOKED = "Not needed — the load was booked before the draft was sent."


def supersede_pending(
    session: Session,
    negotiation_id,
    reason: str,
    *,
    keep_id=None,
) -> int:
    """
    Close every pending draft on a negotiation because it is no longer needed.

    A dispatcher who answers the broker from Gmail never comes back to dismiss
    the draft the agent wrote, so drafts used to pile up forever and the panel
    asked for decisions that had already been made somewhere else.

    `reason` is shown to the dispatcher verbatim, so it is written as a
    sentence rather than a code. `keep_id` spares the draft that is itself
    being sent.
    """
    query = session.query(models.Suggestion).filter(
        models.Suggestion.negotiation_id == negotiation_id,
        models.Suggestion.status == models.PENDING,
    )
    if keep_id is not None:
        query = query.filter(models.Suggestion.id != keep_id)

    stale = query.all()
    if not stale:
        return 0

    for suggestion in stale:
        suggestion.status = models.SUPERSEDED
        suggestion.resolved_reason = reason
        suggestion.resolved_at = datetime.now(timezone.utc)

    session.flush()
    logger.info(
        f"negotiation {negotiation_id}: closed {len(stale)} draft(s) "
        f"no longer needed — {reason}"
    )
    return len(stale)
