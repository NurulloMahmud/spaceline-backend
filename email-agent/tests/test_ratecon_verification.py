"""
The rule the whole feature rests on: a rate confirmation that does not match
the agreed terms must never become a load in the TMS.
"""
import pytest

from database import models
from services import booking, events, inbound
from services.ai import ai
from services.boxtruck import BoxTruckError, boxtruck
from services.nylas_client import nylas

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
    calls = {"booked": [], "telegram": [], "events": []}

    async def fake_download(*a, **k):
        return b"%PDF-1.4 fake"

    async def fake_resolve_broker(**k):
        return {"id": 501, "name": "Acme Logistics", "mc": "123456", "ai_type": "Gemini"}

    async def fake_book_load(payload, ratecon=None):
        calls["booked"].append(payload)
        return {"load_id": 9001, "load_number": payload.get("load_number"), "shipment": 1207}

    async def fake_send_message(chat_id, text):
        calls["telegram"].append((chat_id, text))
        return True

    def fake_publish(event):
        calls["events"].append(event)

    monkeypatch.setattr(nylas, "download_attachment", fake_download)
    monkeypatch.setattr(boxtruck, "resolve_broker", fake_resolve_broker)
    monkeypatch.setattr(boxtruck, "book_load", fake_book_load)
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
        attachments=[{"id": "att-1", "filename": "ratecon.pdf"}],
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

    assert wire["booked"] == [], "a mismatched ratecon must not create a load"
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

    assert len(wire["booked"]) == 1
    assert negotiation.status == models.BOOKED
    assert wire["booked"][0]["carrier_pay"] == 3000.0


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

    assert wire["booked"] == []
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

    assert wire["booked"] == []
    assert negotiation.status == models.MISMATCH


async def test_verified_ratecon_books_and_notifies(
    session, account, negotiation, wire, monkeypatch
):
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

    assert negotiation.status == models.BOOKED
    assert negotiation.tms_load_id == 9001

    payload = wire["booked"][0]
    assert payload["carrier_pay"] == 3200.0, "broker rate goes to carrier_pay"
    assert payload["driver_pay"] == 2400.0, "the driver's own bid goes to driver_pay"
    assert payload["company_id"] == 1
    assert payload["driver_id"] == 42
    assert payload["broker_id"] == 501
    assert payload["load_number"] == "ACME-99871"
    assert len(payload["stops"]) == 2
    assert payload["stops"][0]["load_pickup"] is True
    assert payload["stops"][1]["load_drop"] is True

    chat_id, text = wire["telegram"][0]
    assert chat_id == "-1001234567890"
    assert "Load booked" in text
    assert "2,400" in text, "the driver sees their own rate"
    assert "3,200" not in text, "the driver must not see the broker rate"

    assert events.LOAD_BOOKED in [e.type for e in wire["events"]]

    check = session.query(models.RateconCheck).one()
    assert check.outcome == models.OUTCOME_PASSED


async def test_unreadable_ratecon_alerts_dispatch(
    session, account, negotiation, wire, monkeypatch
):
    stub_ai(monkeypatch, agreed=3200.0, verdict=None)
    error = BoxTruckError("ratecon parse failed (422): unreadable scan")

    await run_ratecon(session, account, negotiation, error, monkeypatch)

    assert wire["booked"] == []
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

    assert wire["booked"] == []
    assert session.query(models.RateconCheck).one().outcome == models.OUTCOME_PARSE_FAILED


async def test_booking_is_idempotent(session, account, negotiation, wire, monkeypatch):
    negotiation.tms_load_id = 9001
    session.flush()

    await booking.book_verified_load(
        session=session,
        negotiation=negotiation,
        parsed=PARSED_RATECON,
        ratecon=("ratecon.pdf", b"%PDF"),
    )

    assert wire["booked"] == [], "an already-booked negotiation must not book twice"


async def test_tms_failure_surfaces_to_dispatch(
    session, account, negotiation, wire, monkeypatch
):
    async def failing_book(payload, ratecon=None):
        raise BoxTruckError("book-load failed (400): company has no shipment number")

    monkeypatch.setattr(boxtruck, "book_load", failing_book)

    await booking.book_verified_load(
        session=session,
        negotiation=negotiation,
        parsed=PARSED_RATECON,
        ratecon=("ratecon.pdf", b"%PDF"),
    )
    session.commit()
    session.refresh(negotiation)

    assert negotiation.status == models.FAILED
    assert "shipment number" in negotiation.failure_reason
    assert events.BOOKING_FAILED in [e.type for e in wire["events"]]
    assert wire["telegram"] == []
