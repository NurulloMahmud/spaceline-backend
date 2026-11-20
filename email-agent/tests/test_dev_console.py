"""Lead events reach only their targets; the developer bid-email health numbers."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from database import models
from services import bid_health, events, outbound
from services.negotiations import NegotiationError, create_negotiation
from tests.test_auth import make_token

SECRET = {"X-Internal-Secret": "test-internal-secret"}


def _event(type_="lead_assigned", company=1, **payload):
    return events.Event(type=type_, company_id=company, payload=payload)


def test_a_targeted_event_reaches_only_its_users():
    e = _event(user_ids=[4, 9])
    assert e.visible_to(1, 4)
    assert e.visible_to(1, 9)
    assert not e.visible_to(1, 5)
    assert not e.visible_to(2, 4)


def test_an_untargeted_event_reaches_the_whole_company():
    e = _event("hiring_changed", driver_ids=[1])
    assert e.visible_to(1, 5)
    assert not e.visible_to(2, 5)


def test_a_lead_without_a_target_list_reaches_nobody():
    # A lead carries a driver's phone and CDL: never company-wide by accident.
    assert not _event(lead={"id": 1}).visible_to(1, 5)
    assert not _event(user_ids="4", lead={"id": 1}).visible_to(1, 4)
    assert not _event("hiring_changed", user_ids=None).visible_to(1, 5)


def test_an_empty_target_list_reaches_nobody():
    assert not _event(user_ids=[]).visible_to(1, 4)


def test_lead_assigned_is_accepted_from_the_internal_route(client, monkeypatch):
    seen = []
    monkeypatch.setattr(events.hub, "publish", seen.append)
    res = client.post(
        "/internal/v1/events",
        json={"type": "lead_assigned", "company_id": 1, "payload": {"user_ids": [4], "reason": "new"}},
        headers=SECRET,
    )
    assert res.status_code == 202
    assert seen[0].type == "lead_assigned"
    assert seen[0].payload["user_ids"] == [4]


def test_lead_assigned_needs_the_secret_and_unknown_types_still_fail(client):
    body = {"type": "lead_assigned", "company_id": 1, "payload": {"user_ids": [4]}}
    assert client.post("/internal/v1/events", json=body).status_code == 401
    assert client.post(
        "/internal/v1/events", json={"type": "nope", "company_id": 1}, headers=SECRET
    ).status_code == 400


# bid email health

def _auth(department="Developer", user_id=1):
    return {"Authorization": "Bearer " + make_token(department=department, user_id=user_id)}


def _neg(session, user, created, sent_after=2, replies=(), load="l"):
    n = models.Negotiation(
        company_id=1, load_uuid=load, load_snapshot={}, dispatcher_user_id=user,
        bid_amount=100, broker_email="b@acme.com", created_at=created,
    )
    session.add(n)
    session.flush()
    session.add(models.EmailMessage(
        negotiation_id=n.id, direction="outbound", from_email="d@x.com",
        created_at=created + timedelta(seconds=sent_after),
    ))
    for sender in replies:
        session.add(models.EmailMessage(
            negotiation_id=n.id, direction="inbound", from_email=sender,
            created_at=created + timedelta(minutes=5),
        ))
    return n


@pytest.fixture
def fixture_data(session):
    now = datetime.now(timezone.utc)
    session.add(models.EmailAccount(
        company_id=1, user_id=7, nylas_grant_id="g7", email_address="d7@gmail.com",
        imap_host="imap.gmail.com",
    ))
    _neg(session, 7, now - timedelta(minutes=10), sent_after=2, replies=("broker@acme.com", "alerts@sylectus.com", "d7@gmail.com"), load="a")
    _neg(session, 7, now - timedelta(minutes=20), sent_after=4, load="b")
    _neg(session, 8, now - timedelta(hours=5), sent_after=6, load="c")
    _neg(session, 8, now - timedelta(days=3), sent_after=6, load="d")
    session.add_all([
        models.BidAttempt(company_id=1, user_id=7, outcome="failed", reason="SMTP refused",
                          created_at=now - timedelta(minutes=3)),
        models.BidAttempt(company_id=1, user_id=9, outcome="blocked", reason="not connected",
                          created_at=now - timedelta(minutes=2)),
        models.BidAttempt(company_id=1, user_id=9, outcome="blocked", reason="not connected",
                          created_at=now - timedelta(hours=30)),
    ])
    session.commit()


def test_one_hour_numbers(client, fixture_data):
    res = client.get("/api/v1/dev/bid-emails?hours=1", headers=_auth())
    assert res.status_code == 200
    data = res.json()
    assert data["window_hours"] == 1
    assert data["totals"] == {"sent": 2, "failed": 1, "blocked": 1, "replies": 1, "median_send_seconds": 3.0}
    assert len(data["series"]) == 12
    assert sum(b["sent"] for b in data["series"]) == 2
    assert sum(b["failed"] for b in data["series"]) == 1
    rows = {r["user_id"]: r for r in data["by_dispatcher"]}
    assert rows[7]["sent"] == 2 and rows[7]["failed"] == 1 and rows[7]["replies"] == 1
    assert rows[7]["mailbox"] == "d7@gmail.com" and rows[7]["provider"] == "gmail"
    assert rows[7]["last_problem"] == "SMTP refused"
    assert rows[9]["last_problem"].startswith("blocked")
    assert rows[9]["name"] is None


def test_day_and_week_buckets(client, fixture_data):
    day = client.get("/api/v1/dev/bid-emails?hours=24", headers=_auth()).json()
    assert len(day["series"]) == 24
    assert day["totals"]["sent"] == 3
    assert day["totals"]["blocked"] == 1
    week = client.get("/api/v1/dev/bid-emails?hours=168", headers=_auth()).json()
    assert len(week["series"]) == 7
    assert week["totals"]["sent"] == 4
    assert week["totals"]["blocked"] == 2


def test_names_come_from_the_users_endpoint(client, fixture_data, monkeypatch):
    from services.boxtruck import boxtruck

    async def people(ids):
        return {7: {"id": 7, "first_name": "Jane", "last_name": "Roe"}}

    monkeypatch.setattr(boxtruck, "get_dispatchers_bulk", people)
    data = client.get("/api/v1/dev/bid-emails?hours=1", headers=_auth()).json()
    assert {r["user_id"]: r["name"] for r in data["by_dispatcher"]}[7] == "Jane Roe"


def test_only_developers_may_read_it(client, fixture_data):
    assert client.get("/api/v1/dev/bid-emails?hours=1", headers=_auth("Management")).status_code == 403
    assert client.get("/api/v1/dev/bid-emails?hours=1", headers=_auth("Dispatch")).status_code == 403
    assert client.get("/api/v1/dev/bid-emails?hours=1").status_code == 401
    assert client.get("/api/v1/dev/bid-emails?hours=5", headers=_auth()).status_code == 422


async def test_a_refused_bid_is_recorded_as_blocked(session, account):
    with pytest.raises(NegotiationError) as err:
        await create_negotiation(
            session, company_id=1, dispatcher_user_id=8, load_uuid="x", bid_amount=1,
        )
    assert err.value.code == "mailbox_not_connected"
    row = session.query(models.BidAttempt).one()
    assert (row.outcome, row.user_id, row.company_id) == ("blocked", 8, 1)
