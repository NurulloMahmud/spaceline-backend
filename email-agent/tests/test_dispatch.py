"""BTM Dispatch: phone tokens checked against Django's Redis key, and the notices sent to Django."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from database import models
from services import auth, dispatch, inbound
from services.ai import ai

DSID = "0b7f2a9e-4c1d-4e8a-9a51-6f3b2c1d0e99"


class FakeRedis:
    def __init__(self, *values):
        # One value per get; the last one repeats.
        self.values = list(values) or [None]
        self.keys = []

    def get(self, key):
        self.keys.append(key)
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


class BrokenRedis:
    def get(self, key):
        raise ConnectionError("redis is down")


def make_token(**overrides) -> str:
    claims = {
        "token_type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "user_id": 7,
        "department": "Dispatch",
        "company": "Shipluxe LLC",
        "company_id": 1,
    }
    claims.update(overrides)
    return jwt.encode(claims, "test-signing-key", algorithm="HS256")


@pytest.fixture
def redis(monkeypatch):
    def use(value):
        monkeypatch.setattr(dispatch, "_redis", value)
        return value

    return use


def test_web_tokens_never_touch_redis(redis):
    redis(BrokenRedis())
    principal = auth.current_user(f"Bearer {make_token()}")
    assert principal.user_id == 7
    assert principal.dsid is None


def test_live_phone_token(redis):
    fake = redis(FakeRedis("active"))
    principal = auth.current_user(f"Bearer {make_token(dsid=DSID)}")
    assert principal.dsid == DSID
    assert principal.user_id == 7
    assert fake.keys == [f"dispatch:dsid:{DSID}"]


@pytest.mark.parametrize(
    "fake, status, code",
    [
        (FakeRedis(None), 401, "device_revoked"),
        (FakeRedis("inactive"), 403, "user_inactive"),
        (BrokenRedis(), 503, "auth_unavailable"),
    ],
)
def test_phone_token_refused(redis, fake, status, code):
    redis(fake)
    with pytest.raises(HTTPException) as caught:
        auth.current_user(f"Bearer {make_token(dsid=DSID)}")
    assert caught.value.status_code == status
    assert caught.value.detail["code"] == code


def test_revoked_phone_on_a_route(client, redis):
    redis(FakeRedis(None))
    reply = client.get(
        "/api/v1/accounts/me", headers={"Authorization": f"Bearer {make_token(dsid=DSID)}"}
    )
    assert reply.status_code == 401
    assert reply.json()["detail"]["code"] == "device_revoked"
    redis(FakeRedis("active"))
    reply = client.get(
        "/api/v1/accounts/me", headers={"Authorization": f"Bearer {make_token(dsid=DSID)}"}
    )
    assert reply.status_code == 200


def test_event_stream_of_a_revoked_phone_ends(client, redis, monkeypatch):
    # Live when the stream opens, gone at the first recheck.
    redis(FakeRedis("active", None))
    monkeypatch.setattr(dispatch, "STREAM_RECHECK_SECONDS", 0)
    lines = []
    with client.stream("GET", f"/api/v1/events/stream?token={make_token(dsid=DSID)}") as reply:
        assert reply.status_code == 200
        for line in reply.iter_lines():
            lines.append(line)
            assert len(lines) < 20, "the stream did not end"
    assert lines[0] == "event: connected"


def test_internal_mailbox_route(client, bidder_mailbox):
    headers = {"X-Internal-Secret": "test-internal-secret"}
    assert client.get("/internal/v1/accounts/users/7").status_code == 401
    mine = client.get("/internal/v1/accounts/users/7", headers=headers).json()
    assert mine["connected"] is True
    assert mine["email_address"] == "dispatcher7@shipluxellc.com"
    assert client.get("/internal/v1/accounts/users/8", headers=headers).json() == {"connected": False}


def test_me_is_unchanged(client, bidder_mailbox):
    reply = client.get("/api/v1/accounts/me", headers={"Authorization": f"Bearer {make_token()}"})
    body = reply.json()
    assert body["enabled"] is True and body["connected"] is True
    assert body["email_address"] == "dispatcher7@shipluxellc.com"


async def test_broker_reply_notifies_the_owner_after_commit(
    session, account, negotiation, dispatch_notices, monkeypatch
):
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "counter_offer", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": 2900, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "counter_offer", "draft_subject": "Re: Bid",
                     "draft_body": "We are holding at $3,200.", "reasoning": "Hold."},
    )
    message = {
        "id": "msg-in-1",
        "thread_id": "thread-1",
        "subject": "Re: Bid",
        "body": "\n  We can do 2900,   can you meet us there?\nThanks, Amy",
        "from": [{"email": "broker@acme-logistics.com"}],
        "attachments": [],
    }
    await inbound.handle_inbound_message(session, account, message)
    assert dispatch_notices == []
    session.commit()
    assert dispatch_notices == [
        {
            "btm_user_id": 7,
            "kind": "broker_reply",
            "title": "Acme Logistics replied",
            "body": "We can do 2900, can you meet us there?",
            "data": {"thread_id": str(negotiation.id), "load_id": "load-uuid-1"},
        }
    ]


def test_rollback_drops_the_notice(session, negotiation, dispatch_notices):
    # As in the inbound path: the message is flushed, then the notice queued.
    negotiation.subject = "Re: Bid"
    session.flush()
    dispatch.broker_replied(session, negotiation, "broker@acme-logistics.com", "Hi")
    negotiation.status = models.RATECON_RECEIVED
    session.rollback()
    session.commit()
    assert dispatch_notices == []


def test_bid_won_once_then_booked(session, negotiation, dispatch_notices):
    negotiation.status = models.NEGOTIATING
    session.commit()
    negotiation.status = models.RATECON_RECEIVED
    session.commit()
    negotiation.status = models.MISMATCH
    session.commit()
    negotiation.status = models.BOOKED
    session.commit()
    data = {"load_id": "load-uuid-1", "bid_id": str(negotiation.id)}
    assert dispatch_notices == [
        {"btm_user_id": 7, "kind": "bid_won", "title": "Your bid won",
         "body": "Chicago, IL to Detroit, MI", "data": data},
        {"btm_user_id": 7, "kind": "bid_booked", "title": "Load booked",
         "body": "Chicago, IL to Detroit, MI", "data": data},
    ]


def test_no_owner_no_notice(session, negotiation, dispatch_notices):
    negotiation.dispatcher_user_id = None
    negotiation.status = models.RATECON_RECEIVED
    session.commit()
    dispatch.broker_replied(session, negotiation, "b@x.com", "Hi")
    session.commit()
    assert dispatch_notices == []


def test_deliver_posts_to_django_and_never_raises(monkeypatch):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers, timeout))
        raise ConnectionError("django is down")

    monkeypatch.setattr(dispatch.config, "BOXTRUCK_BASE_URL", "http://box.test")
    monkeypatch.setattr(dispatch.config, "BOXTRUCK_INTERNAL_SECRET", "box-secret")
    monkeypatch.setattr(dispatch.httpx, "post", post)
    dispatch._deliver({"btm_user_id": 7, "kind": "bid_won"})
    assert calls == [(
        "http://box.test/api/dispatch-app/internal/notify/",
        {"btm_user_id": 7, "kind": "bid_won"},
        {"X-Internal-Secret": "box-secret"},
        3,
    )]


def test_first_line():
    assert dispatch.first_line("\n\n  Hello   there \nsecond") == "Hello there"
    assert dispatch.first_line(None) == ""
