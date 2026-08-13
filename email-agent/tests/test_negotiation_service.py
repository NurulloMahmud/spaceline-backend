import pytest

from database import models
from services import events
from services.atrek import atrek
from services.boxtruck import boxtruck
from services.negotiations import (
    NegotiationError, create_negotiation, extract_broker_email,
)
from services.nylas_client import NylasError, nylas


async def _async(value):
    return value


@pytest.fixture
def stubs(monkeypatch, load_snapshot, company_profile, driver_profile):
    recorded = {"bids": []}

    async def fake_record_bid(**kwargs):
        recorded["bids"].append(kwargs)
        return True

    monkeypatch.setattr(atrek, "get_load", lambda uuid: _async(load_snapshot))
    monkeypatch.setattr(atrek, "record_dispatcher_bid", fake_record_bid)
    monkeypatch.setattr(boxtruck, "get_company", lambda cid: _async(company_profile))
    monkeypatch.setattr(boxtruck, "get_dispatcher", lambda uid: _async({"full_name": "Jane"}))
    monkeypatch.setattr(boxtruck, "get_driver", lambda did: _async(driver_profile))
    monkeypatch.setattr(events.hub, "publish", lambda e: None)
    return recorded


def test_broker_email_is_found_wherever_the_feed_puts_it():
    assert extract_broker_email({"contact_email": "a@b.com"}) == "a@b.com"
    assert extract_broker_email({"email": "c@d.com"}) == "c@d.com"
    assert extract_broker_email({"contact": {"email": "e@f.com"}}) == "e@f.com"
    assert extract_broker_email({"contact_name": "Acme"}) is None
    assert extract_broker_email({"contact_email": "not-an-address"}) is None


async def test_a_send_failure_leaves_no_negotiation_behind(
    session, account, stubs, monkeypatch
):
    async def failing_send(**kwargs):
        raise NylasError("mailbox rejected the message")

    monkeypatch.setattr(nylas, "send_message", failing_send)

    with pytest.raises(NegotiationError) as e:
        await create_negotiation(
            session=session,
            company_id=1,
            dispatcher_user_id=7,
            load_uuid="load-uuid-1",
            bid_amount=3200,
        )
    assert e.value.status_code == 502

    session.rollback()
    assert session.query(models.Negotiation).count() == 0, (
        "a bid that never reached the broker must not look open"
    )


async def test_the_dispatcher_bid_is_recorded_on_the_board(
    session, account, stubs, monkeypatch
):
    monkeypatch.setattr(
        nylas, "send_message",
        lambda **k: _async({"id": "msg-1", "thread_id": "thread-9"}),
    )

    negotiation = await create_negotiation(
        session=session,
        company_id=1,
        dispatcher_user_id=7,
        load_uuid="load-uuid-1",
        bid_amount=3200,
        driver_id=42,
        driver_amount=2400,
    )
    session.commit()

    assert negotiation.nylas_thread_id == "thread-9"
    assert negotiation.driver_telegram_group_id == "-1001234567890"

    bid = stubs["bids"][0]
    assert bid["company_id"] == 1
    assert bid["user_id"] == 7
    assert bid["driver_id"] == 42
    assert bid["amount"] == 3200
    assert bid["driver_amount"] == 2400


async def test_a_board_failure_does_not_undo_a_sent_email(
    session, account, stubs, monkeypatch
):
    """The email is already gone; the negotiation must still exist."""
    monkeypatch.setattr(
        nylas, "send_message",
        lambda **k: _async({"id": "msg-1", "thread_id": "thread-9"}),
    )
    monkeypatch.setattr(atrek, "record_dispatcher_bid", lambda **k: _async(False))

    negotiation = await create_negotiation(
        session=session,
        company_id=1,
        dispatcher_user_id=7,
        load_uuid="load-uuid-1",
        bid_amount=3200,
    )
    session.commit()

    assert negotiation.status == models.BID_SENT
    assert session.query(models.Negotiation).count() == 1


async def test_an_unreachable_load_board_blocks_the_bid(session, account, stubs, monkeypatch):
    monkeypatch.setattr(atrek, "get_load", lambda uuid: _async(None))

    with pytest.raises(NegotiationError) as e:
        await create_negotiation(
            session=session,
            company_id=1,
            dispatcher_user_id=7,
            load_uuid="load-uuid-1",
            bid_amount=3200,
        )
    assert e.value.code == "load_unavailable"
