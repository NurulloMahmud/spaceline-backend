"""
The Nylas webhook endpoint, exercised over HTTP the way Nylas calls it.

Nylas delivers here directly — no peer service sits in front — so these tests
cover the whole contract: challenge verification, the signature that is the
endpoint's only credential, the v3 payload shape, and what happens to a
delivery that fails midway.
"""
import hashlib
import hmac
import json

import pytest

from database import models
from services import inbound
from services.nylas_client import NylasError, nylas
from tests.test_auth import make_token

SECRET = b"test-webhook-secret"
WEBHOOK_URL = "/internal/v1/webhooks/nylas"


def sign(body: bytes) -> str:
    return hmac.new(SECRET, body, hashlib.sha256).hexdigest()


def delivery(trigger: str, obj: dict, grant_id: str | None = None) -> bytes:
    """A Nylas v3 webhook body, in the shape Nylas actually sends."""
    data: dict = {"application_id": "4ee94420-d0d6-46f3-b789-999263f0e18d", "object": obj}
    if grant_id:
        data["grant_id"] = grant_id
    return json.dumps(
        {
            "specversion": "1.0",
            "type": trigger,
            "source": "/google/emails/realtime",
            "id": "evt-1",
            "time": 1787827767,
            "webhook_delivery_attempt": 1,
            "data": data,
        }
    ).encode()


def post(client, body: bytes, signature: str | None = None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Nylas-Signature"] = signature
    return client.post(WEBHOOK_URL, content=body, headers=headers)


@pytest.fixture
def message_created():
    return delivery(
        "message.created",
        {"id": "msg-1", "grant_id": "grant-1", "thread_id": "thread-1"},
    )


@pytest.fixture
def capture(monkeypatch):
    """Stub Nylas and the pipeline; record what the router handed each one."""
    calls: dict = {"fetched": [], "inbound": [], "own_send": []}

    async def get_message(grant_id, message_id):
        calls["fetched"].append((grant_id, message_id))
        return {
            "id": message_id,
            "grant_id": grant_id,
            "thread_id": "thread-1",
            "subject": "Re: Bid",
            "from": [{"email": "broker@acme-logistics.com"}],
            "body": "We can do 3200.",
        }

    async def handle_inbound_message(session, account, message):
        calls["inbound"].append(message["id"])

    async def handle_own_send(session, account, message):
        calls["own_send"].append(message["id"])

    monkeypatch.setattr(nylas, "get_message", get_message)
    monkeypatch.setattr(inbound, "handle_inbound_message", handle_inbound_message)
    monkeypatch.setattr(inbound, "handle_own_send", handle_own_send)
    return calls


# --- endpoint verification -------------------------------------------------

def test_challenge_is_echoed_as_plain_text(client):
    """Nylas refuses to register a webhook whose endpoint fails this."""
    resp = client.get(WEBHOOK_URL, params={"challenge": "nylas-challenge-abc"})
    assert resp.status_code == 200
    assert resp.text == "nylas-challenge-abc"
    assert resp.headers["content-type"].startswith("text/plain")


# --- the signature is the only credential ----------------------------------

def test_an_unsigned_delivery_is_rejected(client, message_created, capture):
    assert post(client, message_created).status_code == 401
    assert capture["fetched"] == []


def test_a_wrongly_signed_delivery_is_rejected(client, message_created, capture):
    assert post(client, message_created, "deadbeef").status_code == 401
    assert capture["fetched"] == []


def test_a_tampered_body_is_rejected(client, message_created, capture):
    """The signature covers the raw body, so swapping the id invalidates it."""
    signature = sign(message_created)
    tampered = message_created.replace(b'"msg-1"', b'"msg-evil"')
    assert post(client, tampered, signature).status_code == 401
    assert capture["fetched"] == []


# --- message.created -------------------------------------------------------

def test_a_broker_reply_is_fetched_and_processed(client, account, message_created, capture):
    resp = post(client, message_created, sign(message_created))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert capture["fetched"] == [("grant-1", "msg-1")]
    assert capture["inbound"] == ["msg-1"]
    assert capture["own_send"] == []


def test_the_grant_id_may_arrive_beside_the_object(client, account, capture):
    """Nylas puts grant_id on data for some triggers and on the object for others."""
    body = delivery("message.created", {"id": "msg-2"}, grant_id="grant-1")
    assert post(client, body, sign(body)).status_code == 200
    assert capture["fetched"] == [("grant-1", "msg-2")]


def test_our_own_send_is_recorded_not_treated_as_a_reply(
    client, account, message_created, capture, monkeypatch
):
    async def get_message(grant_id, message_id):
        return {
            "id": message_id,
            "thread_id": "thread-1",
            "from": [{"email": "DISPATCH@ShipLuxeLLC.com"}],
            "body": "Sent from Gmail.",
        }

    monkeypatch.setattr(nylas, "get_message", get_message)
    assert post(client, message_created, sign(message_created)).status_code == 200
    assert capture["own_send"] == ["msg-1"]
    assert capture["inbound"] == []


def test_a_redelivery_is_processed_only_once(client, account, message_created, capture, session):
    signature = sign(message_created)
    assert post(client, message_created, signature).status_code == 200
    assert post(client, message_created, signature).status_code == 200

    assert capture["inbound"] == ["msg-1"]
    assert session.query(models.ProcessedWebhook).count() == 1


def test_a_message_for_an_unknown_grant_is_dropped(client, capture, session):
    """No account has this grant — nothing to attribute the mail to."""
    body = delivery("message.created", {"id": "msg-9", "grant_id": "grant-nobody"})
    assert post(client, body, sign(body)).status_code == 200
    assert capture["fetched"] == []
    assert session.query(models.ProcessedWebhook).count() == 0


@pytest.mark.parametrize(
    "obj", [{"grant_id": "grant-1"}, {"id": "msg-3"}, {}]
)
def test_an_incomplete_delivery_is_ignored(client, account, capture, obj):
    body = delivery("message.created", obj)
    resp = post(client, body, sign(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": "incomplete"}
    assert capture["fetched"] == []


def test_a_trigger_we_do_not_subscribe_to_is_ignored(client, account, capture):
    body = delivery("message.opened", {"id": "msg-4", "grant_id": "grant-1"})
    resp = post(client, body, sign(body))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "ignored": "message.opened"}
    assert capture["fetched"] == []


# --- failures must stay retryable -----------------------------------------

def test_a_failed_fetch_leaves_the_message_for_nylas_to_retry(
    client, account, message_created, capture, session, monkeypatch
):
    """
    Claiming the message before processing it is what makes concurrent
    redeliveries safe — but the claim has to die with the transaction when
    the work fails, or Nylas's retry finds the message already 'processed'
    and the broker's reply is lost for good.
    """
    working = nylas.get_message      # the stub `capture` installed
    attempts = {"n": 0}

    async def fails_once(grant_id, message_id):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise NylasError("503 from Nylas")
        return await working(grant_id, message_id)

    monkeypatch.setattr(nylas, "get_message", fails_once)

    assert post(client, message_created, sign(message_created)).status_code == 200
    assert capture["inbound"] == []
    assert session.query(models.ProcessedWebhook).count() == 0

    # Nylas retries the same message; this time it lands.
    assert post(client, message_created, sign(message_created)).status_code == 200
    assert capture["inbound"] == ["msg-1"]
    assert session.query(models.ProcessedWebhook).count() == 1


def test_a_failure_inside_the_pipeline_also_stays_retryable(
    client, account, message_created, capture, session, monkeypatch
):
    async def boom(session_, account_, message):
        raise RuntimeError("the model timed out")

    monkeypatch.setattr(inbound, "handle_inbound_message", boom)
    assert post(client, message_created, sign(message_created)).status_code == 200
    assert session.query(models.ProcessedWebhook).count() == 0


# --- grant lifecycle -------------------------------------------------------

@pytest.mark.parametrize(
    "trigger,expected",
    [("grant.expired", "expired"), ("grant.deleted", "revoked")],
)
def test_a_dead_grant_flags_the_mailbox(client, account, session, trigger, expected):
    body = delivery(trigger, {"grant_id": "grant-1", "provider": "google"})
    assert post(client, body, sign(body)).status_code == 200

    session.refresh(account)
    assert account.status == expected


def test_a_dead_grant_is_reported_to_dispatch(client, account, session):
    """The settings page must stop showing a dead mailbox as connected."""
    body = delivery("grant.expired", {"grant_id": "grant-1"})
    assert post(client, body, sign(body)).status_code == 200
    session.refresh(account)

    resp = client.get(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {make_token()}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"


def test_a_dead_grant_for_an_unknown_mailbox_is_harmless(client, account, session):
    body = delivery("grant.deleted", {"grant_id": "grant-nobody"})
    assert post(client, body, sign(body)).status_code == 200
    session.refresh(account)
    assert account.status == "active"


def test_a_grant_event_without_a_grant_id_is_ignored(client, account, session):
    body = delivery("grant.expired", {})
    resp = post(client, body, sign(body))
    assert resp.json() == {"ok": True, "ignored": "incomplete"}
    session.refresh(account)
    assert account.status == "active"
