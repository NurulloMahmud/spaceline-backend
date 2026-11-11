"""
Dispatcher decisions on drafted replies. A suggestion only reaches a broker
through send(), and only when a dispatcher calls it.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from database import models

logger = logging.getLogger(__name__)


def reply_target(session: Session, negotiation: models.Negotiation, source_id=None) -> Optional[str]:
    """
    The RFC Message-ID a new email on this negotiation answers: the message
    the dispatcher is replying to when known, else the broker's latest, else
    our own latest, else the posting email the negotiation started from.
    Every mail client threads on it, whichever mailbox holds the message.
    """
    if source_id is not None:
        source = session.get(models.EmailMessage, source_id)
        if source is not None and source.rfc_message_id:
            return source.rfc_message_id
    messages = (
        session.query(models.EmailMessage)
        .filter(models.EmailMessage.negotiation_id == negotiation.id)
        .order_by(models.EmailMessage.created_at.desc())
        .all()
    )
    ordered = [m for m in messages if m.direction == "inbound"] + [m for m in messages if m.direction != "inbound"]
    rfc_id = next((m.rfc_message_id for m in ordered if m.rfc_message_id), None)
    if rfc_id:
        return rfc_id
    return (negotiation.load_snapshot or {}).get("email_message_id") or None


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
