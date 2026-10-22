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


POSTING = {
    "email_message_id": "<posting-1@mail.gmail.com>",
    "email_subject": "LARGE STRAIGHT from Lakeland, FL to Brookfield, CT - Posted by SWICK",
}


async def _bid(session):
    return await create_negotiation(
        session=session,
        company_id=1,
        dispatcher_user_id=7,
        load_uuid="load-uuid-1",
        bid_amount=3200,
    )


async def test_bid_replies_to_the_posting_email(
    session, account, stubs, monkeypatch, load_snapshot
):
    load_snapshot.update(POSTING)
    sent = []

    async def fake_find(grant_id, rfc_id):
        assert rfc_id == "<posting-1@mail.gmail.com>"
        return {"id": "nylas-posting-1", "subject": POSTING["email_subject"]}

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "thread-posting"}

    monkeypatch.setattr(nylas, "find_message_by_rfc_id", fake_find)
    monkeypatch.setattr(nylas, "send_message", fake_send)

    negotiation = await _bid(session)

    assert sent[0]["reply_to_message_id"] == "nylas-posting-1"
    assert sent[0]["subject"] == "Re: " + POSTING["email_subject"]
    assert negotiation.subject == sent[0]["subject"]
    assert negotiation.nylas_thread_id == "thread-posting"


async def test_bid_starts_a_new_email_when_the_posting_is_not_in_the_mailbox(
    session, account, stubs, monkeypatch, load_snapshot
):
    load_snapshot.update(POSTING)
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "thread-new"}

    monkeypatch.setattr(nylas, "find_message_by_rfc_id", lambda g, r: _async(None))
    monkeypatch.setattr(nylas, "send_message", fake_send)

    await _bid(session)

    assert sent[0]["reply_to_message_id"] is None
    assert sent[0]["subject"].startswith("Bid — ")


async def test_a_load_without_a_posting_email_never_searches_the_mailbox(
    session, account, stubs, monkeypatch
):
    async def boom(grant_id, rfc_id):
        raise AssertionError("should not search")

    async def fake_send(**kwargs):
        return {"id": "msg-out-1", "thread_id": "t"}

    monkeypatch.setattr(nylas, "find_message_by_rfc_id", boom)
    monkeypatch.setattr(nylas, "send_message", fake_send)

    await _bid(session)
