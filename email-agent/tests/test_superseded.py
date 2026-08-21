"""
Drafts the conversation moved past.

A dispatcher who answers the broker from their mail client never comes back to
dismiss the draft the agent wrote. Left alone those drafts accumulated until
the panel was asking for decisions that had already been made in Gmail.
"""
from datetime import datetime, timedelta, timezone

import pytest

from database import models
from services import inbound, maintenance, suggestions


def make_suggestion(session, negotiation, status=models.PENDING, minutes_ago=10):
    s = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_REPLY,
        intent="counter_offer",
        draft_subject=f"Re: {negotiation.subject}",
        draft_body="We are holding at $3,200.",
        ai_reasoning="Hold the rate.",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    session.add(s)
    session.commit()
    return s


def make_outbound(session, negotiation, *, by_user, minutes_ago=1):
    m = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=f"m-{datetime.now(timezone.utc).timestamp()}-{by_user}",
        direction="outbound",
        from_email="dispatch@shipluxellc.com",
        to_email=negotiation.broker_email,
        subject="Re: Bid",
        body_text="Handled.",
        sent_by_user_id=by_user,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    session.add(m)
    session.commit()
    return m


# --- the core rule ---------------------------------------------------------


def test_replying_from_a_mail_client_closes_the_drafts(session, account, negotiation):
    """The reported problem: drafts hung forever after replying in Gmail."""
    a = make_suggestion(session, negotiation)
    b = make_suggestion(session, negotiation)

    closed = suggestions.supersede_pending(
        session, negotiation.id, suggestions.REPLIED_ELSEWHERE
    )
    session.commit()

    assert closed == 2
    for s in (a, b):
        session.refresh(s)
        assert s.status == models.SUPERSEDED
        assert "mail client" in s.resolved_reason


def test_a_superseded_draft_is_not_an_ignored_one(session, account, negotiation):
    """`ignored` is a person's decision; `superseded` is the system's."""
    s = make_suggestion(session, negotiation)
    suggestions.supersede_pending(session, negotiation.id, suggestions.REPLIED_ELSEWHERE)
    session.commit()
    session.refresh(s)

    assert s.status == models.SUPERSEDED
    assert s.status != models.IGNORED
    assert s.resolved_by_user_id is None      # nobody clicked anything
    assert s.resolved_reason                   # but it says why


def test_resolved_drafts_are_left_alone(session, account, negotiation):
    sent = make_suggestion(session, negotiation, status=models.SENT)
    ignored = make_suggestion(session, negotiation, status=models.IGNORED)

    assert suggestions.supersede_pending(
        session, negotiation.id, suggestions.REPLIED_ELSEWHERE) == 0
    session.refresh(sent); session.refresh(ignored)
    assert sent.status == models.SENT
    assert ignored.status == models.IGNORED


def test_keep_id_spares_the_draft_being_sent(session, account, negotiation):
    keep = make_suggestion(session, negotiation)
    other = make_suggestion(session, negotiation)

    closed = suggestions.supersede_pending(
        session, negotiation.id, suggestions.REPLIED_IN_APP, keep_id=keep.id)
    session.commit()

    assert closed == 1
    session.refresh(keep); session.refresh(other)
    assert keep.status == models.PENDING
    assert other.status == models.SUPERSEDED


def test_one_negotiation_does_not_affect_another(session, account, negotiation, load_snapshot):
    other = models.Negotiation(
        company_id=1, load_uuid="load-two", load_snapshot=load_snapshot,
        dispatcher_user_id=7, bid_amount=100.0,
        broker_email="b@x.com", subject="Other", status=models.NEGOTIATING)
    session.add(other); session.commit()

    mine = make_suggestion(session, negotiation)
    theirs = make_suggestion(session, other)

    suggestions.supersede_pending(session, negotiation.id, suggestions.REPLIED_ELSEWHERE)
    session.commit()

    session.refresh(mine); session.refresh(theirs)
    assert mine.status == models.SUPERSEDED
    assert theirs.status == models.PENDING


# --- wired into the real paths ---------------------------------------------


async def test_out_of_app_reply_closes_pending_drafts(session, account, negotiation):
    stale = make_suggestion(session, negotiation)

    await inbound.handle_own_send(session, account, {
        "id": "msg-from-gmail",
        "thread_id": negotiation.nylas_thread_id,
        "subject": "Re: Bid",
        "body": "<div>We can do $3,000.</div>",
        "from": [{"email": account.email_address}],
        "to": [{"email": negotiation.broker_email}],
        "attachments": [],
    })
    session.commit()
    session.refresh(stale)

    assert stale.status == models.SUPERSEDED
    assert "mail client" in stale.resolved_reason


def test_closing_a_negotiation_closes_its_drafts(session, account, negotiation):
    stale = make_suggestion(session, negotiation)

    negotiation.status = models.CLOSED
    suggestions.supersede_pending(session, negotiation.id, suggestions.NEGOTIATION_CLOSED)
    session.commit()
    session.refresh(stale)

    assert stale.status == models.SUPERSEDED
    assert "closed" in stale.resolved_reason


# --- the safety net --------------------------------------------------------


def test_reconciler_closes_drafts_answered_afterwards(session, account, negotiation):
    """Catches anything the live paths missed."""
    stale = make_suggestion(session, negotiation, minutes_ago=30)
    make_outbound(session, negotiation, by_user=None, minutes_ago=5)   # from Gmail

    assert maintenance.close_answered_drafts(session) == 1
    session.commit()
    session.refresh(stale)
    assert stale.status == models.SUPERSEDED
    assert "answered" in stale.resolved_reason


def test_reconciler_leaves_genuinely_waiting_drafts_alone(session, account, negotiation):
    """A draft with no reply after it is still the dispatcher's to decide."""
    waiting = make_suggestion(session, negotiation, minutes_ago=5)
    make_outbound(session, negotiation, by_user=7, minutes_ago=30)  # reply came BEFORE

    assert maintenance.close_answered_drafts(session) == 0
    session.refresh(waiting)
    assert waiting.status == models.PENDING


def test_reconciler_is_idempotent(session, account, negotiation):
    make_suggestion(session, negotiation, minutes_ago=30)
    make_outbound(session, negotiation, by_user=None, minutes_ago=5)

    assert maintenance.close_answered_drafts(session) == 1
    session.commit()
    assert maintenance.close_answered_drafts(session) == 0
