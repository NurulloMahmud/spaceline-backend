"""
Broker-response numbers for a period: how many bids we sent and how many drew
at least one email back from the broker.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import case, exists, func
from sqlalchemy.orm import Session

from database import models

# The posting email lives in the same thread as our bid, so it shows up as an
# inbound message from here. It is not the broker answering.
POSTING_SENDER_DOMAIN = "sylectus.com"


def _rate(replied: int, sent: int) -> Optional[float]:
    return round(100.0 * replied / sent, 1) if sent else None


def broker_response_stats(
    session: Session, company_id: int, start: datetime, end: datetime
) -> dict:
    """
    Bids are the negotiations created in [start, end]. A bid counts as replied
    when it has an inbound email that is neither the Sylectus posting nor sent
    from one of the company's own mailboxes. Automatic replies count: they
    cannot be told apart reliably.
    """
    # The company mailbox plus every dispatcher's own: mail from any of them
    # is ours, not a broker answering.
    own_addresses = [
        address.lower()
        for (address,) in session.query(models.EmailAccount.email_address)
        .filter(models.EmailAccount.company_id == company_id)
        .all()
        if address
    ]

    sender = func.lower(func.coalesce(models.EmailMessage.from_email, ""))
    from_broker = [
        models.EmailMessage.negotiation_id == models.Negotiation.id,
        models.EmailMessage.direction == "inbound",
        sender.notlike(f"%{POSTING_SENDER_DOMAIN}"),
    ]
    if own_addresses:
        from_broker.append(sender.notin_(own_addresses))
    replied = exists().where(*from_broker)

    rows = (
        session.query(
            models.Negotiation.dispatcher_user_id,
            func.count(models.Negotiation.id),
            func.sum(case((replied, 1), else_=0)),
        )
        .filter(
            models.Negotiation.company_id == company_id,
            models.Negotiation.created_at >= start,
            models.Negotiation.created_at <= end,
        )
        .group_by(models.Negotiation.dispatcher_user_id)
        .all()
    )

    dispatchers = [
        {
            "id": user_id,
            "bids_sent": int(sent),
            "broker_replied": int(got or 0),
            "reply_rate": _rate(int(got or 0), int(sent)),
        }
        for user_id, sent, got in rows
    ]
    dispatchers.sort(key=lambda d: d["bids_sent"], reverse=True)

    bids_sent = sum(d["bids_sent"] for d in dispatchers)
    broker_replied = sum(d["broker_replied"] for d in dispatchers)
    return {
        "bids_sent": bids_sent,
        "broker_replied": broker_replied,
        "reply_rate": _rate(broker_replied, bids_sent),
        "dispatchers": dispatchers,
    }
