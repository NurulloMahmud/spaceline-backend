"""Auto-closing unanswered bids, and holding drivers while a bid is live."""
from datetime import datetime, timedelta, timezone

import pytest

from config.settings import config
from database import models
from services import events, maintenance


@pytest.fixture
def captured(monkeypatch):
    published = []
    monkeypatch.setattr(events.hub, "publish", lambda e: published.append(e))
    return published


def make_negotiation(session, load_snapshot, *, status, age_minutes, driver_id=42):
    n = models.Negotiation(
        company_id=1,
        load_uuid=f"load-{status}-{age_minutes}-{driver_id}",
        load_snapshot=load_snapshot,
        driver_id=driver_id,
        driver_amount=2400.0,
        dispatcher_user_id=7,
        bid_amount=3200.0,
        broker_email="broker@acme-logistics.com",
        subject="Bid — Chicago to Detroit",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    )
    session.add(n)
    session.commit()
    return n


# --- auto-close ------------------------------------------------------------


def test_unanswered_bid_is_closed_after_the_wait(session, load_snapshot, captured):
    n = make_negotiation(session, load_snapshot, status=models.BID_SENT, age_minutes=31)

    closed = maintenance.close_stale_bids(session)
    session.commit()
    session.refresh(n)

    assert [c.id for c in closed] == [n.id]
    assert n.status == models.CLOSED
    assert "did not reply within 30 minutes" in n.failure_reason
    assert [e.type for e in captured] == [events.NEGOTIATION_UPDATED]


def test_a_recent_bid_is_left_alone(session, load_snapshot, captured):
    n = make_negotiation(session, load_snapshot, status=models.BID_SENT, age_minutes=29)

    assert maintenance.close_stale_bids(session) == []
    session.refresh(n)
    assert n.status == models.BID_SENT
    assert captured == []


def test_a_live_conversation_is_never_auto_closed(session, load_snapshot):
    """Once the broker replies, only a dispatcher ends the negotiation."""
    old = make_negotiation(session, load_snapshot, status=models.NEGOTIATING, age_minutes=600)

    assert maintenance.close_stale_bids(session) == []
    session.refresh(old)
    assert old.status == models.NEGOTIATING


def test_booked_negotiations_are_never_auto_closed(session, load_snapshot):
    n = make_negotiation(session, load_snapshot, status=models.BOOKED, age_minutes=600)
    assert maintenance.close_stale_bids(session) == []
    session.refresh(n)
    assert n.status == models.BOOKED


# --- driver holds ----------------------------------------------------------


def test_driver_with_a_live_bid_is_held(session, load_snapshot):
    make_negotiation(session, load_snapshot, status=models.BID_SENT, age_minutes=1, driver_id=42)
    assert maintenance.held_driver_ids(session) == [42]


def test_hold_lifts_once_the_window_passes(session, load_snapshot):
    """Only applies to a bid the broker has not answered."""
    make_negotiation(
        session, load_snapshot, status=models.BID_SENT,
        age_minutes=config.DRIVER_HOLD_MINUTES + 1, driver_id=42,
    )
    assert maintenance.held_driver_ids(session) == []


def test_a_negotiating_driver_is_held_with_no_time_limit(session, load_snapshot):
    """
    Once the broker engages, the hold lasts as long as the conversation does.
    Offering this driver other freight is how one truck gets double-booked.
    """
    make_negotiation(
        session, load_snapshot, status=models.NEGOTIATING,
        age_minutes=600, driver_id=42,
    )
    assert maintenance.held_driver_ids(session) == [42]


def test_a_driver_awaiting_ratecon_check_is_held(session, load_snapshot):
    make_negotiation(
        session, load_snapshot, status=models.RATECON_RECEIVED,
        age_minutes=600, driver_id=77,
    )
    assert maintenance.held_driver_ids(session) == [77]


def test_hold_lifts_as_soon_as_the_bid_closes(session, load_snapshot):
    """'or until the bid is closed' — a closed bid releases the driver at once."""
    n = make_negotiation(
        session, load_snapshot, status=models.BID_SENT, age_minutes=1, driver_id=42
    )
    assert maintenance.held_driver_ids(session) == [42]

    n.status = models.CLOSED
    session.commit()
    assert maintenance.held_driver_ids(session) == []


def test_a_negotiation_still_running_holds_the_driver(session, load_snapshot):
    make_negotiation(
        session, load_snapshot, status=models.NEGOTIATING, age_minutes=1, driver_id=51
    )
    assert maintenance.held_driver_ids(session) == [51]


def test_a_booked_negotiation_stops_holding(session, load_snapshot):
    """
    A booked load is the TMS's business: the bot skips that driver via the
    busy-drivers list, which knows when the load is actually finished.
    """
    make_negotiation(
        session, load_snapshot, status=models.BOOKED, age_minutes=1, driver_id=42
    )
    assert maintenance.held_driver_ids(session) == []


def test_negotiations_without_a_driver_hold_nobody(session, load_snapshot):
    n = make_negotiation(session, load_snapshot, status=models.BID_SENT, age_minutes=1)
    n.driver_id = None
    session.commit()
    assert maintenance.held_driver_ids(session) == []
