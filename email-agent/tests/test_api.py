"""End-to-end through the HTTP layer, with peer services stubbed."""
import pytest

from database import models
from services.atrek import atrek
from services.boxtruck import boxtruck
from services.nylas_client import nylas
from tests.test_auth import make_token


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {make_token()}"}


@pytest.fixture
def stub_peers(monkeypatch, load_snapshot, company_profile, driver_profile):
    sent = []

    async def fake_get_load(load_uuid):
        return load_snapshot

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "thread-1"}

    async def fake_record_bid(**kwargs):
        return True

    monkeypatch.setattr(atrek, "get_load", fake_get_load)
    monkeypatch.setattr(atrek, "record_dispatcher_bid", fake_record_bid)
    monkeypatch.setattr(nylas, "send_message", fake_send)
    monkeypatch.setattr(boxtruck, "get_company", lambda cid: _async(company_profile))
    monkeypatch.setattr(boxtruck, "get_dispatcher", lambda uid: _async({"full_name": "Jane Dispatcher"}))
    monkeypatch.setattr(boxtruck, "get_driver", lambda did: _async(driver_profile))
    return sent


async def _async(value):
    return value


def test_health_needs_no_auth(client):
    assert client.get("/api/v1/health").json()["success"] is True


def test_endpoints_require_a_token(client):
    assert client.get("/api/v1/negotiations").status_code == 401
    assert client.get("/api/v1/suggestions").status_code == 401
    assert client.get("/api/v1/accounts").status_code == 401
    assert client.post(
        "/api/v1/negotiations", json={"load_uuid": "x", "bid_amount": 1}
    ).status_code == 401
    assert client.get("/api/v1/events/stream").status_code == 401


def test_bidding_without_a_mailbox_is_refused(client, auth_headers, stub_peers):
    resp = client.post(
        "/api/v1/negotiations",
        headers=auth_headers,
        json={"load_uuid": "load-uuid-1", "bid_amount": 3200},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "mailbox_not_connected"


def test_bid_sends_the_email_and_opens_the_thread(
    client, auth_headers, account, stub_peers, session
):
    resp = client.post(
        "/api/v1/negotiations",
        headers=auth_headers,
        json={
            "load_uuid": "load-uuid-1",
            "bid_amount": 3200,
            "driver_id": 42,
            "driver_amount": 2400,
            "driver_bid_id": "bid-1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "bid_sent"
    assert body["bid_amount"] == 3200
    assert body["broker_email"] == "broker@acme-logistics.com"

    assert len(stub_peers) == 1
    email = stub_peers[0]
    assert email["to_email"] == "broker@acme-logistics.com"
    assert "RATE: $3,200" in email["body"]
    assert "2,400" not in email["body"], "the driver's bid must not reach the broker"

    stored = session.query(models.EmailMessage).one()
    assert stored.direction == "outbound"
    assert stored.nylas_message_id == "msg-out-1"


def test_a_load_without_a_broker_email_asks_for_one(
    client, auth_headers, account, stub_peers, monkeypatch, load_snapshot
):
    stripped = {k: v for k, v in load_snapshot.items() if k != "contact_email"}
    monkeypatch.setattr(atrek, "get_load", lambda uuid: _async(stripped))

    resp = client.post(
        "/api/v1/negotiations",
        headers=auth_headers,
        json={"load_uuid": "load-uuid-1", "bid_amount": 3200},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "broker_email_required"

    # supplying one lets it through
    resp = client.post(
        "/api/v1/negotiations",
        headers=auth_headers,
        json={
            "load_uuid": "load-uuid-1",
            "bid_amount": 3200,
            "broker_email": "dispatch@acme-logistics.com",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["broker_email"] == "dispatch@acme-logistics.com"


def test_one_open_negotiation_per_load(client, auth_headers, account, stub_peers):
    payload = {"load_uuid": "load-uuid-1", "bid_amount": 3200}
    assert client.post("/api/v1/negotiations", headers=auth_headers, json=payload).status_code == 201
    second = client.post("/api/v1/negotiations", headers=auth_headers, json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "already_open"


def test_negotiations_are_company_scoped(client, auth_headers, session, negotiation):
    """A dispatcher at company 2 cannot see company 1's negotiation."""
    other = {"Authorization": f"Bearer {make_token(company='Smart Fleet LLC', company_id=2)}"}

    assert client.get("/api/v1/negotiations", headers=auth_headers).json()["total"] == 1
    assert client.get("/api/v1/negotiations", headers=other).json()["total"] == 0

    assert client.get(f"/api/v1/negotiations/{negotiation.id}", headers=auth_headers).status_code == 200
    assert client.get(f"/api/v1/negotiations/{negotiation.id}", headers=other).status_code == 404


def test_suggestions_are_company_scoped(client, auth_headers, session, negotiation):
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_REPLY,
        draft_subject="Re: Bid",
        draft_body="We can do $3,200.",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.commit()

    other = {"Authorization": f"Bearer {make_token(company='Smart Fleet LLC', company_id=2)}"}
    assert client.get("/api/v1/suggestions", headers=auth_headers).json()["total"] == 1
    assert client.get("/api/v1/suggestions", headers=other).json()["total"] == 0

    resp = client.post(f"/api/v1/suggestions/{suggestion.id}/ignore", headers=other)
    assert resp.status_code == 404, "another company must not be able to resolve this"


def test_ignoring_a_suggestion_resolves_it(client, auth_headers, session, negotiation):
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_REPLY,
        draft_body="draft",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.commit()

    resp = client.post(f"/api/v1/suggestions/{suggestion.id}/ignore", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == models.IGNORED

    again = client.post(f"/api/v1/suggestions/{suggestion.id}/ignore", headers=auth_headers)
    assert again.status_code == 409


def test_sending_a_suggestion_replies_on_the_thread(
    client, auth_headers, account, session, negotiation, monkeypatch
):
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-2", "thread_id": "thread-1"}

    monkeypatch.setattr(nylas, "send_message", fake_send)

    inbound_msg = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id="msg-in-1",
        direction="inbound",
        body_text="Can you do 2900?",
    )
    session.add(inbound_msg)
    session.flush()
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        in_reply_to_message_id=inbound_msg.id,
        kind=models.KIND_REPLY,
        draft_subject="Re: Bid",
        draft_body="Our rate is $3,200.",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.commit()

    resp = client.post(
        f"/api/v1/suggestions/{suggestion.id}/send", headers=auth_headers, json={}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == models.SENT
    assert sent[0]["reply_to_message_id"] == "msg-in-1"
    assert "Our rate is $3,200." in sent[0]["body"]


def test_editing_before_sending_is_recorded_separately(
    client, auth_headers, account, session, negotiation, monkeypatch
):
    monkeypatch.setattr(
        nylas, "send_message",
        lambda **k: _async({"id": "msg-out-3", "thread_id": "thread-1"}),
    )
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_REPLY,
        draft_subject="Re: Bid",
        draft_body="Our rate is $3,200.",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.commit()

    resp = client.post(
        f"/api/v1/suggestions/{suggestion.id}/send",
        headers=auth_headers,
        json={"body": "Best we can do is $3,150."},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == models.EDITED_SENT
    assert resp.json()["final_body"] == "Best we can do is $3,150."


def test_an_empty_body_is_not_sent(
    client, auth_headers, account, session, negotiation
):
    suggestion = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_PARSE_FAILURE,
        draft_body="",
        status=models.PENDING,
    )
    session.add(suggestion)
    session.commit()

    resp = client.post(
        f"/api/v1/suggestions/{suggestion.id}/send", headers=auth_headers, json={}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "empty_body"


def test_booked_negotiations_cannot_be_closed(
    client, auth_headers, session, negotiation
):
    negotiation.status = models.BOOKED
    session.commit()
    resp = client.post(f"/api/v1/negotiations/{negotiation.id}/close", headers=auth_headers)
    assert resp.status_code == 409


# --- filtering the negotiations list by who sent the bid --------------------


@pytest.fixture
def two_dispatchers_bids(session, load_snapshot):
    """User 7 is the caller in auth_headers; user 99 is a colleague."""
    for i, (user_id, load) in enumerate([(7, "load-mine"), (99, "load-theirs")]):
        session.add(
            models.Negotiation(
                company_id=1,
                load_uuid=load,
                load_snapshot=load_snapshot,
                driver_id=40 + i,
                dispatcher_user_id=user_id,
                bid_amount=3200.0,
                broker_email="broker@acme-logistics.com",
                subject=f"Bid {load}",
                status=models.BID_SENT,
            )
        )
    session.commit()


def test_all_negotiations_by_default(client, auth_headers, two_dispatchers_bids):
    body = client.get("/api/v1/negotiations", headers=auth_headers).json()
    assert body["total"] == 2


def test_mine_true_returns_only_my_bids(client, auth_headers, two_dispatchers_bids):
    body = client.get("/api/v1/negotiations?mine=true", headers=auth_headers).json()
    assert body["total"] == 1
    assert body["items"][0]["load_uuid"] == "load-mine"
    assert body["items"][0]["dispatcher_user_id"] == 7


def test_mine_false_returns_only_other_dispatchers(client, auth_headers, two_dispatchers_bids):
    body = client.get("/api/v1/negotiations?mine=false", headers=auth_headers).json()
    assert body["total"] == 1
    assert body["items"][0]["load_uuid"] == "load-theirs"


def test_filter_by_a_named_dispatcher(client, auth_headers, two_dispatchers_bids):
    body = client.get(
        "/api/v1/negotiations?dispatcher_user_id=99", headers=auth_headers
    ).json()
    assert body["total"] == 1
    assert body["items"][0]["dispatcher_user_id"] == 99


def test_mine_combines_with_status(client, auth_headers, two_dispatchers_bids):
    body = client.get(
        "/api/v1/negotiations?mine=true&status=bid_sent", headers=auth_headers
    ).json()
    assert body["total"] == 1

    body = client.get(
        "/api/v1/negotiations?mine=true&status=booked", headers=auth_headers
    ).json()
    assert body["total"] == 0


def test_mine_still_cannot_cross_companies(client, session, load_snapshot):
    """Company scoping is not weakened by the new filter."""
    from tests.test_auth import make_token

    session.add(
        models.Negotiation(
            company_id=2, load_uuid="other-co", load_snapshot=load_snapshot,
            dispatcher_user_id=7, bid_amount=100.0,
            broker_email="b@x.com", subject="Other co", status=models.BID_SENT,
        )
    )
    session.commit()

    headers = {"Authorization": f"Bearer {make_token(company_id=1)}"}
    body = client.get("/api/v1/negotiations?mine=true", headers=headers).json()
    assert all(i["load_uuid"] != "other-co" for i in body["items"])


# --- the thread view does not replay the dispatcher's own sends -------------


def _suggestion(session, negotiation, status, body="Holding at $3,200."):
    s = models.Suggestion(
        negotiation_id=negotiation.id,
        kind=models.KIND_REPLY,
        intent="counter_offer",
        draft_subject=f"Re: {negotiation.subject}",
        draft_body=body,
        ai_reasoning="",
        status=status,
        resolved_by_user_id=7 if status != models.PENDING else None,
    )
    session.add(s)
    session.commit()
    return s


def test_sent_suggestions_are_not_replayed_in_the_thread(
    client, session, auth_headers, negotiation
):
    """
    Sending a draft already puts the email in `messages`. Returning the
    suggestion too made the dispatcher's own action look like a second event.
    """
    _suggestion(session, negotiation, models.SENT)
    _suggestion(session, negotiation, models.EDITED_SENT)

    body = client.get(f"/api/v1/negotiations/{negotiation.id}", headers=auth_headers).json()
    assert body["suggestions"] == []


def test_pending_and_ignored_suggestions_still_show(
    client, session, auth_headers, negotiation
):
    """Pending needs a decision; ignored records that we chose not to reply."""
    _suggestion(session, negotiation, models.PENDING)
    _suggestion(session, negotiation, models.IGNORED)
    _suggestion(session, negotiation, models.SENT)

    body = client.get(f"/api/v1/negotiations/{negotiation.id}", headers=auth_headers).json()
    statuses = sorted(s["status"] for s in body["suggestions"])
    assert statuses == ["ignored", "pending"]
    assert body["pending_suggestions"] == 1


def test_sent_suggestions_remain_in_the_suggestions_list(
    client, session, auth_headers, negotiation
):
    """Hiding them from the thread must not lose the record."""
    _suggestion(session, negotiation, models.SENT)

    body = client.get("/api/v1/suggestions?status=sent", headers=auth_headers).json()
    assert body["total"] == 1


def test_superseded_drafts_leave_the_thread_and_the_badge(
    client, session, auth_headers, negotiation
):
    """
    The reported problem, seen through the API: drafts the conversation moved
    past must stop asking for a decision.
    """
    from services import suggestions as suggestion_service

    _suggestion(session, negotiation, models.PENDING)
    _suggestion(session, negotiation, models.PENDING)
    session.commit()

    before = client.get(f"/api/v1/negotiations/{negotiation.id}", headers=auth_headers).json()
    assert before["pending_suggestions"] == 2
    assert len(before["suggestions"]) == 2

    suggestion_service.supersede_pending(
        session, negotiation.id, suggestion_service.REPLIED_ELSEWHERE)
    session.commit()

    after = client.get(f"/api/v1/negotiations/{negotiation.id}", headers=auth_headers).json()
    assert after["pending_suggestions"] == 0
    assert after["suggestions"] == []
    assert after["superseded_suggestions"] == 2


def test_a_superseded_draft_still_says_why(client, session, auth_headers, negotiation):
    """The panel should never have to explain itself."""
    from services import suggestions as suggestion_service

    _suggestion(session, negotiation, models.PENDING)
    suggestion_service.supersede_pending(
        session, negotiation.id, suggestion_service.REPLIED_ELSEWHERE)
    session.commit()

    body = client.get("/api/v1/suggestions?status=superseded", headers=auth_headers).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["status"] == "superseded"
    assert "mail client" in item["resolved_reason"]
