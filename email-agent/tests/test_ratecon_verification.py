"""
A rate confirmation is checked against the agreed terms, and never becomes a
load in the TMS by itself: dispatchers book loads by hand, even on a match.
"""
import pytest

from database import models
from services import events, inbound
from services.ai import ai
from services.boxtruck import BoxTruckError, boxtruck

PARSED_RATECON = {
    "broker_name": "Acme Logistics",
    "load_number": "ACME-99871",
    "total_rate_usd": 3200.0,
    "pickup_addresses": [{
        "facility": "Acme DC",
        "city": "Chicago",
        "state": "IL",
        "zip_code": "60601",
        "address": "200 N Michigan Ave",
        "driver_instructions": "Dock 4",
        "date_time": "2026-08-01 09:00",
    }],
    "delivery_locations": [{
        "facility": "Motor City Whse",
        "city": "Detroit",
        "state": "MI",
        "zip_code": "48201",
        "address": "1400 Woodward Ave",
        "driver_instructions": "Appointment required",
        "date_time": "2026-08-02 14:00",
    }],
    "special_instructions": "No pallet exchange.",
}


@pytest.fixture
def wire(monkeypatch, session):
    """Stubs every outbound call and records what the pipeline tried to do."""
    calls = {"telegram": [], "events": []}

    async def fake_resolve_broker(**k):
        return {"id": 501, "name": "Acme Logistics", "mc": "123456", "ai_type": "Gemini"}

    async def fake_send_message(chat_id, text):
        calls["telegram"].append((chat_id, text))
        return True

    def fake_publish(event):
        calls["events"].append(event)

    monkeypatch.setattr(boxtruck, "resolve_broker", fake_resolve_broker)
    monkeypatch.setattr("services.telegram.send_message", fake_send_message)
    monkeypatch.setattr(events.hub, "publish", fake_publish)
    return calls


def stub_ai(monkeypatch, *, agreed, verdict):
    monkeypatch.setattr(
        ai, "extract_agreed_rate",
        lambda *a, **k: {"agreed_amount": agreed, "confident": agreed is not None, "evidence": ""},
    )
    monkeypatch.setattr(ai, "verify_ratecon", lambda *a, **k: verdict)
    monkeypatch.setattr(
        ai, "draft_mismatch_reply",
        lambda *a, **k: {
            "draft_subject": "Re: Bid",
            "draft_body": "The rate confirmation shows a different rate than we agreed.",
            "reasoning": "Price does not match.",
        },
    )


async def run_ratecon(session, account, negotiation, parsed, monkeypatch):
    monkeypatch.setattr(boxtruck, "parse_ratecon", lambda **k: _resolve(parsed))
    message = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id="msg-ratecon",
        direction="inbound",
        from_email="broker@acme-logistics.com",
        body_text="Attached is the rate confirmation.",
        has_attachments=True,
    )
    session.add(message)
    session.flush()

    await inbound.handle_ratecon(
        session=session,
        account=account,
        negotiation=negotiation,
        message_row=message,
        attachments=[{"id": "att-1", "filename": "ratecon.pdf", "content": b"%PDF-1.4 fake"}],
        nylas_message_id="msg-ratecon",
    )
    session.commit()
    session.refresh(negotiation)


async def _resolve(value):
    if isinstance(value, Exception):
        raise value
    return value


async def test_price_mismatch_blocks_booking(
    session, account, negotiation, wire, monkeypatch
):
    """Agreed $3,200, ratecon says $3,000 — no load, a suggested reply instead."""
    parsed = {**PARSED_RATECON, "total_rate_usd": 3000.0}
    stub_ai(
        monkeypatch,
        agreed=3200.0,
        verdict={
            "price_ok": False,
            "ratecon_amount": 3000.0,
            "locations_ok": True,
            "dates_ok": True,
            "discrepancies": [],
        },
    )

    await run_ratecon(session, account, negotiation, parsed, monkeypatch)

    assert negotiation.tms_load_id is None, "a mismatched ratecon must not create a load"
    assert negotiation.status == models.MISMATCH
    assert negotiation.tms_load_id is None

    check = session.query(models.RateconCheck).one()
    assert check.outcome == models.OUTCOME_MISMATCH
    assert check.price_ok is False
    assert check.agreed_amount == 3200.0
    assert check.ratecon_amount == 3000.0
    assert any("3,000" in d and "3,200" in d for d in check.discrepancies)

    suggestion = session.query(models.Suggestion).one()
    assert suggestion.kind == models.KIND_MISMATCH
    assert suggestion.status == models.PENDING

    assert [e.type for e in wire["events"]] == [events.RATECON_MISMATCH]
    assert wire["telegram"] == [], "the driver must not be told about an unbooked load"


async def test_price_agreement_is_read_from_the_thread(
    session, account, negotiation, wire, monkeypatch
):
    """We opened at $3,200 but settled at $3,000 in email — that ratecon is valid."""
    parsed = {**PARSED_RATECON, "total_rate_usd": 3000.0}
    stub_ai(
        monkeypatch,
        agreed=3000.0,
        verdict={
            "price_ok": True,
            "ratecon_amount": 3000.0,
            "locations_ok": True,
            "dates_ok": True,
            "discrepancies": [],
        },
    )

    await run_ratecon(session, account, negotiation, parsed, monkeypatch)

    assert negotiation.agreed_amount == 3000.0
    assert session.query(models.RateconCheck).one().outcome == models.OUTCOME_PASSED
    assert negotiation.tms_load_id is None


async def test_location_mismatch_blocks_booking(
    session, account, negotiation, wire, monkeypatch
):
    stub_ai(
        monkeypatch,
        agreed=3200.0,
        verdict={
            "price_ok": True,
            "ratecon_amount": 3200.0,
            "locations_ok": False,
            "dates_ok": True,
            "discrepancies": ["Delivery is Cleveland, OH on the ratecon but Detroit, MI was posted."],
        },
    )

    await run_ratecon(session, account, negotiation, PARSED_RATECON, monkeypatch)

    assert negotiation.tms_load_id is None
    assert negotiation.status == models.MISMATCH
    assert session.query(models.RateconCheck).one().locations_ok is False


async def test_date_mismatch_blocks_booking(
    session, account, negotiation, wire, monkeypatch
):
    stub_ai(
        monkeypatch,
        agreed=3200.0,
        verdict={
            "price_ok": True,
            "ratecon_amount": 3200.0,
            "locations_ok": True,
            "dates_ok": False,
            "discrepancies": ["Pickup is 2026-08-03 on the ratecon but 2026-08-01 was posted."],
        },
    )

    await run_ratecon(session, account, negotiation, PARSED_RATECON, monkeypatch)

    assert negotiation.tms_load_id is None
    assert negotiation.status == models.MISMATCH


async def test_verified_ratecon_creates_no_load(
    session, account, negotiation, wire, monkeypatch
):
    """A matching ratecon is recorded as checked; booking stays with dispatch."""
    stub_ai(
        monkeypatch,
        agreed=3200.0,
        verdict={
            "price_ok": True,
            "ratecon_amount": 3200.0,
            "locations_ok": True,
            "dates_ok": True,
            "discrepancies": [],
        },
    )

    await run_ratecon(session, account, negotiation, PARSED_RATECON, monkeypatch)

    assert not hasattr(boxtruck, "book_load"), "the agent must have no way to create a load"
    assert negotiation.status == models.RATECON_RECEIVED
    assert negotiation.tms_load_id is None
    assert wire["telegram"] == [], "the driver is not told about a load nobody booked"
    assert wire["events"] == []
    assert session.query(models.Suggestion).count() == 0
    check = session.query(models.RateconCheck).one()
    assert check.outcome == models.OUTCOME_PASSED


async def test_unreadable_ratecon_alerts_dispatch(
    session, account, negotiation, wire, monkeypatch
):
    stub_ai(monkeypatch, agreed=3200.0, verdict=None)
    error = BoxTruckError("ratecon parse failed (422): unreadable scan")

    await run_ratecon(session, account, negotiation, error, monkeypatch)

    assert negotiation.tms_load_id is None
    check = session.query(models.RateconCheck).one()
    assert check.outcome == models.OUTCOME_PARSE_FAILED
    assert "unreadable scan" in check.error

    suggestion = session.query(models.Suggestion).one()
    assert suggestion.kind == models.KIND_PARSE_FAILURE
    assert suggestion.status == models.PENDING
    assert [e.type for e in wire["events"]] == [events.RATECON_PARSE_FAILED]


async def test_failed_verification_call_does_not_book(
    session, account, negotiation, wire, monkeypatch
):
    """If the model cannot return a verdict we alert, we do not assume it passed."""
    stub_ai(monkeypatch, agreed=3200.0, verdict=None)

    await run_ratecon(session, account, negotiation, PARSED_RATECON, monkeypatch)

    assert negotiation.tms_load_id is None
    assert session.query(models.RateconCheck).one().outcome == models.OUTCOME_PARSE_FAILED
