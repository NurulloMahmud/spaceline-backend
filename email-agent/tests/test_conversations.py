"""
Conversations: what is new is tracked per user, and only the dispatcher who
sent the bid can write, at any time, whatever the negotiation's status.
"""
from datetime import datetime, timedelta, timezone

import pytest

from database import models
from services import conversations
from services import outbound
from tests.test_auth import make_token


@pytest.fixture(autouse=True)
def reads_since_long_ago(monkeypatch):
    """The messages here are minutes old; keep them after the cut-over whatever the clock says."""
    monkeypatch.setattr(conversations, "READS_SINCE", datetime(2020, 1, 1, tzinfo=timezone.utc))


def _headers(user_id=7):
    return {"Authorization": f"Bearer {make_token(user_id=user_id)}"}


def _message(session, negotiation, direction, minutes_ago, **extra):
    m = models.EmailMessage(
        negotiation_id=negotiation.id,
        direction=direction,
        from_email="broker@acme-logistics.com" if direction == "inbound" else "dispatch@shipluxellc.com",
        to_email="dispatch@shipluxellc.com" if direction == "inbound" else "broker@acme-logistics.com",
        subject="Re: Bid — Chicago, IL to Detroit, MI",
        body_text=extra.pop("body_text", f"{direction} {minutes_ago}"),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        **extra,
    )
    session.add(m)
    session.commit()
    return m


@pytest.fixture
def sends(monkeypatch):
    calls = []

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        calls.append(kwargs)
        return {"id": f"sent-{len(calls)}", "thread_id": "thread-1"}

    monkeypatch.setattr(outbound, "send", fake_send)
    return calls


@pytest.fixture
def other(session, load_snapshot):
    """A second conversation, bid by another dispatcher (user 8)."""
    n = models.Negotiation(
        company_id=1,
        load_uuid="load-uuid-2",
        load_snapshot=load_snapshot,
        dispatcher_user_id=8,
        bid_amount=1500.0,
        broker_email="ops@other-broker.com",
        subject="Bid — Dallas, TX to Austin, TX",
        status=models.CLOSED,
    )
    session.add(n)
    session.commit()
    return n


def test_broker_messages_are_new_until_opened_and_per_user(client, session, negotiation, account):
    _message(session, negotiation, "outbound", 30, sent_by_user_id=7)
    _message(session, negotiation, "inbound", 20)
    _message(session, negotiation, "inbound", 10, body_text="Can you do 2900?\n\nThanks")

    item = client.get("/api/v1/conversations", headers=_headers(7)).json()["items"][0]
    assert item["unread"] == 2
    assert item["can_write"] is True
    assert item["last_message"]["direction"] == "inbound"
    assert item["last_message"]["text"] == "Can you do 2900? Thanks"

    assert client.post(f"/api/v1/conversations/{negotiation.id}/read", headers=_headers(7)).status_code == 200
    assert client.get("/api/v1/conversations", headers=_headers(7)).json()["items"][0]["unread"] == 0
    # A manager opening it does not read it for the dispatcher, nor the reverse.
    assert client.get("/api/v1/conversations", headers=_headers(1)).json()["items"][0]["unread"] == 2

    _message(session, negotiation, "inbound", 0)
    assert client.get("/api/v1/conversations", headers=_headers(7)).json()["items"][0]["unread"] == 1


def test_messages_from_before_read_tracking_are_not_new(client, session, negotiation, account, monkeypatch):
    monkeypatch.setattr(conversations, "READS_SINCE", datetime.now(timezone.utc) - timedelta(minutes=30))
    _message(session, negotiation, "inbound", 60)
    _message(session, negotiation, "inbound", 10)
    assert client.get("/api/v1/conversations", headers=_headers(7)).json()["items"][0]["unread"] == 1


def test_latest_activity_first_and_filters(client, session, negotiation, other, account):
    _message(session, negotiation, "inbound", 50)
    _message(session, other, "inbound", 5)

    items = client.get("/api/v1/conversations", headers=_headers(7)).json()["items"]
    assert [i["id"] for i in items] == [str(other.id), str(negotiation.id)]
    assert [i["can_write"] for i in items] == [False, True]

    mine = client.get("/api/v1/conversations?who=me", headers=_headers(7)).json()
    assert [i["id"] for i in mine["items"]] == [str(negotiation.id)]
    theirs = client.get("/api/v1/conversations?who=8", headers=_headers(7)).json()
    assert [i["id"] for i in theirs["items"]] == [str(other.id)]

    client.post(f"/api/v1/conversations/{other.id}/read", headers=_headers(7))
    unread = client.get("/api/v1/conversations?unread=true", headers=_headers(7)).json()
    assert [i["id"] for i in unread["items"]] == [str(negotiation.id)]

    # The menu badge counts only the caller's own conversations.
    assert client.get("/api/v1/conversations/unread", headers=_headers(7)).json() == {"mine": 1}
    assert client.get("/api/v1/conversations/unread", headers=_headers(8)).json() == {"mine": 1}


def test_the_dispatcher_writes_on_the_thread_at_any_time(client, session, negotiation, account, sends):
    negotiation.status = models.CLOSED
    session.add(models.Suggestion(negotiation_id=negotiation.id, draft_body="draft", status=models.PENDING))
    session.commit()
    _message(session, negotiation, "outbound", 30, sent_by_user_id=7, nylas_message_id="bid-1")
    _message(session, negotiation, "inbound", 10, nylas_message_id="broker-1", rfc_message_id="r1@acme")

    resp = client.post(
        f"/api/v1/conversations/{negotiation.id}/messages",
        json={"body": "We can do $2,950.\n<b>Thanks</b>"},
        headers=_headers(7),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["direction"] == "outbound" and resp.json()["sent_by_user_id"] == 7

    (call,) = sends
    assert call["grant_id"] == "grant-1"
    assert call["to_email"] == "broker@acme-logistics.com"
    assert call["subject"] == "Re: Bid — Chicago, IL to Detroit, MI"
    assert call["in_reply_to"] == "r1@acme"
    assert call["body"] == "We can do $2,950.<br>&lt;b&gt;Thanks&lt;/b&gt;"

    session.expire_all()
    assert negotiation.status == models.NEGOTIATING
    assert {s.status for s in negotiation.suggestions} == {models.SUPERSEDED}
    stored = [m for m in negotiation.messages if m.nylas_message_id == "sent-1"]
    assert stored and stored[0].body_text == "We can do $2,950.\n<b>Thanks</b>"
    # Writing is reading.
    assert client.get("/api/v1/conversations", headers=_headers(7)).json()["items"][0]["unread"] == 0


def test_others_can_read_but_not_write(client, session, negotiation, account, sends):
    resp = client.post(
        f"/api/v1/conversations/{negotiation.id}/messages", json={"body": "hi"}, headers=_headers(8)
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "not_your_conversation"
    assert sends == []


def test_a_reply_threads_on_the_brokers_latest_message_id(client, session, negotiation, account, sends):
    _message(session, negotiation, "inbound", 10, nylas_message_id="old-grant-msg", rfc_message_id="<r1@acme>")
    resp = client.post(
        f"/api/v1/conversations/{negotiation.id}/messages", json={"body": "Still available?"}, headers=_headers(7)
    )
    assert resp.status_code == 201, resp.text
    (call,) = sends
    assert call["in_reply_to"] == "<r1@acme>"


def test_a_disconnected_personal_mailbox_cannot_write(client, session, negotiation, sends):
    own = models.EmailAccount(
        company_id=1, user_id=7, nylas_grant_id="grant-own-7", email_address="jane@gmail.com", status="revoked"
    )
    session.add(own)
    session.commit()
    negotiation.email_account_id = own.id
    session.commit()

    resp = client.post(
        f"/api/v1/conversations/{negotiation.id}/messages", json={"body": "hi"}, headers=_headers(7)
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "mailbox_not_connected"
    assert sends == []


def test_empty_messages_are_refused(client, negotiation, account, sends):
    resp = client.post(
        f"/api/v1/conversations/{negotiation.id}/messages", json={"body": "   "}, headers=_headers(7)
    )
    assert resp.status_code == 422
    assert sends == []
