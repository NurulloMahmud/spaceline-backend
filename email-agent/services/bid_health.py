"""
Bid email health for the developer console: what went out, what failed, what
was refused because the dispatcher had no connected mailbox, and how many
brokers answered.

sent     negotiations with an outbound message, by the time it was written
failed   BidAttempt rows with outcome "failed" (the send failed; the request
         rolls the negotiation back, so only this table remembers it)
blocked  BidAttempt rows with outcome "blocked" (no connected mailbox). Both
         counts start from the day the table was created; nothing older exists.
replies  inbound messages on bids created in the window, not the Sylectus
         posting and not from one of the company's own mailboxes
median   first outbound message time minus negotiation created time
"""
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import models
from database.connection import get_session
from services.stats import POSTING_SENDER_DOMAIN

logger = logging.getLogger(__name__)

# window hours -> (bucket size, bucket count)
WINDOWS = {
    1: (timedelta(minutes=5), 12),
    24: (timedelta(hours=1), 24),
    168: (timedelta(days=1), 7),
}


def record_attempt(
    company_id: int,
    user_id: Optional[int],
    outcome: str,
    reason: str,
    *,
    mailbox: Optional[str] = None,
    load_uuid: Optional[str] = None,
) -> None:
    """Own session, because the request's session rolls back on the error that
    follows. Never raises: losing a health row must not change the bid's answer."""
    try:
        with get_session() as session:
            session.add(
                models.BidAttempt(
                    company_id=company_id,
                    user_id=user_id,
                    outcome=outcome,
                    reason=(reason or "")[:1000],
                    mailbox=mailbox,
                    load_uuid=load_uuid,
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("could not record a bid attempt")


def _floor(moment: datetime, step: timedelta) -> datetime:
    seconds = int(step.total_seconds())
    stamp = int(moment.timestamp())
    return datetime.fromtimestamp(stamp - stamp % seconds, tz=timezone.utc)


def _provider(address: Optional[str], imap_host: Optional[str]) -> Optional[str]:
    host = (imap_host or "").lower()
    domain = (address or "").split("@")[-1].lower()
    if "gmail" in host or "google" in host or domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if "outlook" in host or "office365" in host or domain in ("outlook.com", "hotmail.com", "live.com"):
        return "microsoft"
    if "yahoo" in host or domain.startswith("yahoo."):
        return "yahoo"
    if "icloud" in host or domain in ("icloud.com", "me.com"):
        return "icloud"
    return domain or None


def compute(session: Session, company_id: int, hours: int, now: Optional[datetime] = None) -> dict:
    step, count = WINDOWS[hours]
    now = now or datetime.now(timezone.utc)
    last = _floor(now, step)
    first = last - step * (count - 1)
    starts = [first + step * i for i in range(count)]
    buckets = {s: {"start": s.isoformat(), "sent": 0, "failed": 0} for s in starts}

    def bucket_of(moment: datetime) -> Optional[dict]:
        return buckets.get(_floor(moment, step))

    # Sent: negotiations whose first outbound message was written in the window.
    first_out = (
        session.query(
            models.Negotiation.id,
            models.Negotiation.dispatcher_user_id,
            models.Negotiation.created_at,
            func.min(models.EmailMessage.created_at).label("sent_at"),
        )
        .join(models.EmailMessage, models.EmailMessage.negotiation_id == models.Negotiation.id)
        .filter(
            models.Negotiation.company_id == company_id,
            # A bid's first email goes out seconds after it is made; a day of margin
            # keeps the scan to recent bids instead of the whole history.
            models.Negotiation.created_at >= first - timedelta(days=1),
            models.EmailMessage.direction == "outbound",
        )
        .group_by(models.Negotiation.id)
        .having(func.min(models.EmailMessage.created_at) >= first)
        .all()
    )
    per_user: dict = {}

    def user_row(user_id):
        return per_user.setdefault(
            user_id,
            {"user_id": user_id, "sent": 0, "failed": 0, "replies": 0,
             "last_problem": None, "last_problem_at": None},
        )

    delays = []
    for row in first_out:
        b = bucket_of(row.sent_at)
        if b is not None:
            b["sent"] += 1
        user_row(row.dispatcher_user_id)["sent"] += 1
        delays.append((row.sent_at - row.created_at).total_seconds())

    failed = blocked = 0
    attempts = (
        session.query(models.BidAttempt)
        .filter(models.BidAttempt.company_id == company_id, models.BidAttempt.created_at >= first)
        .order_by(models.BidAttempt.created_at)
        .all()
    )
    for a in attempts:
        u = user_row(a.user_id)
        if a.outcome == models.ATTEMPT_FAILED:
            failed += 1
            u["failed"] += 1
            b = bucket_of(a.created_at)
            if b is not None:
                b["failed"] += 1
        else:
            blocked += 1
        u["last_problem"] = (
            a.reason if a.outcome == models.ATTEMPT_FAILED else f"blocked: {a.reason}"
        )
        u["last_problem_at"] = a.created_at.isoformat()

    # Replies on bids created in the window.
    accounts = (
        session.query(models.EmailAccount).filter(models.EmailAccount.company_id == company_id).all()
    )
    own = {a.email_address.lower() for a in accounts if a.email_address}
    sender = func.lower(func.coalesce(models.EmailMessage.from_email, ""))
    reply_q = (
        session.query(models.Negotiation.dispatcher_user_id, func.count(models.EmailMessage.id))
        .join(models.EmailMessage, models.EmailMessage.negotiation_id == models.Negotiation.id)
        .filter(
            models.Negotiation.company_id == company_id,
            models.Negotiation.created_at >= first,
            models.EmailMessage.direction == "inbound",
            sender.notlike(f"%{POSTING_SENDER_DOMAIN}"),
        )
    )
    if own:
        reply_q = reply_q.filter(sender.notin_(own))
    replies = 0
    for user_id, n in reply_q.group_by(models.Negotiation.dispatcher_user_id).all():
        user_row(user_id)["replies"] += n
        replies += n

    mailboxes = {a.user_id: a for a in accounts if a.user_id is not None}
    by_dispatcher = []
    for user_id, u in per_user.items():
        acct = mailboxes.get(user_id)
        u["mailbox"] = acct.email_address if acct else None
        u["provider"] = _provider(acct.email_address, acct.imap_host) if acct else None
        u["name"] = None
        by_dispatcher.append(u)
    by_dispatcher.sort(key=lambda u: (-(u["sent"] + u["failed"]), u["user_id"] or 0))

    return {
        "window_hours": hours,
        "totals": {
            "sent": len(first_out),
            "failed": failed,
            "blocked": blocked,
            "replies": replies,
            "median_send_seconds": round(statistics.median(delays), 1) if delays else None,
        },
        "series": [buckets[s] for s in starts],
        "by_dispatcher": by_dispatcher,
    }
