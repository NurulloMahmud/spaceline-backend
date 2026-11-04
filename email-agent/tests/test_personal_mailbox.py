"""
A dispatcher's own mailbox: connected from their profile, used for their
bids, and kept apart from the company's shared dispatch mailbox. The bid
replies to the Sylectus alert by Message-ID, since the alert never reached
the personal mailbox.
"""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from config.settings import config
from database import models
from routers.accounts import _sign, _sign_personal
from services import inbound
from services.negotiations import create_negotiation
from services.nylas_client import nylas
from tests.test_auth import make_token
from tests.test_negotiation_service import POSTING, stubs  # noqa: F401


@pytest.fixture
def nylas_configured(monkeypatch):
    monkeypatch.setattr(config, "NYLAS_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "NYLAS_CALLBACK_URI", "https://example.com/callback")


@pytest.fixture
def rolled_out(monkeypatch):
    monkeypatch.setattr(config, "PERSONAL_MAILBOX_USERS", "7")


@pytest.fixture
def own_account(session):
    acct = models.EmailAccount(
        company_id=1,
        user_id=7,
        nylas_grant_id="grant-own-7",
        email_address="jane@gmail.com",
    )
    session.add(acct)
    session.commit()
    return acct


def _headers(**claims):
    return {"Authorization": f"Bearer {make_token(**claims)}"}


def _redirect(resp):
    assert resp.status_code == 302, resp.text
    location = urlparse(resp.headers["location"])
    return location.path, {k: v[0] for k, v in parse_qs(location.query).items()}


def test_the_card_stays_hidden_until_rolled_out(client):
    resp = client.get("/api/v1/accounts/me", headers=_headers())
    assert resp.json() == {"enabled": False, "connected": False}


def test_star_rolls_it_out_to_everyone(monkeypatch):
    monkeypatch.setattr(config, "PERSONAL_MAILBOX_USERS", "*")
    assert config.personal_mailbox_allowed(12345)


def test_a_user_outside_the_rollout_cannot_connect(client, nylas_configured, rolled_out):
    resp = client.post("/api/v1/accounts/me/connect", headers=_headers(user_id=8))
    assert resp.status_code == 403


def test_a_rolled_out_user_gets_a_state_naming_them(client, nylas_configured, rolled_out):
    resp = client.post("/api/v1/accounts/me/connect", headers=_headers())
    assert resp.status_code == 200
    query = parse_qs(urlparse(resp.json()["auth_url"]).query)
    assert query["state"][0].startswith("u7-1.")


def test_connecting_stores_a_mailbox_for_that_user(client, monkeypatch, session, account):
    async def fake_exchange(code):
        return {"grant_id": "grant-own-7", "email": "jane@gmail.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign_personal(7, 1)},
        follow_redirects=False,
    )

    path, params = _redirect(resp)
    assert path == "/profile"
    assert params["mailbox"] == "connected"
    own = session.query(models.EmailAccount).filter_by(user_id=7).one()
    assert (own.company_id, own.email_address, own.status) == (1, "jane@gmail.com", "active")
    # The shared mailbox is untouched.
    shared = session.query(models.EmailAccount).filter_by(company_id=1, user_id=None).one()
    assert shared.nylas_grant_id == "grant-1"


def test_the_shared_mailbox_cannot_be_claimed_as_a_personal_one(
    client, monkeypatch, session, account
):
    revoked = []

    async def fake_exchange(code):
        return {"grant_id": "grant-1", "email": "dispatch@shipluxellc.com"}

    async def fake_revoke(grant_id):
        revoked.append(grant_id)

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)
    monkeypatch.setattr(nylas, "revoke_grant", fake_revoke)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign_personal(7, 1)},
        follow_redirects=False,
    )

    _, params = _redirect(resp)
    assert params == {"mailbox": "error", "reason": "mailbox_in_use"}
    assert revoked == []
    assert session.query(models.EmailAccount).filter_by(user_id=7).count() == 0


def test_a_forged_personal_state_is_refused(client):
    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": "u7-1.00000000000000000000000000000000"},
        follow_redirects=False,
    )
    path, params = _redirect(resp)
    assert path == "/profile"
    assert params["reason"] == "invalid_state"


def test_reconnecting_the_shared_mailbox_leaves_personal_ones_alone(
    client, monkeypatch, session, account, own_account
):
    async def fake_exchange(code):
        return {"grant_id": "grant-shared-2", "email": "dispatch@shipluxellc.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1)},
        follow_redirects=False,
    )

    _, params = _redirect(resp)
    assert params["mailbox"] == "connected"
    session.expire_all()
    assert session.get(models.EmailAccount, account.id).nylas_grant_id == "grant-shared-2"
    assert session.get(models.EmailAccount, own_account.id).nylas_grant_id == "grant-own-7"


def test_company_status_reports_the_shared_mailbox_only(client, session, own_account):
    resp = client.get("/api/v1/accounts", headers=_headers())
    assert resp.json() == {"connected": False}


def test_disconnecting_revokes_and_hides_the_mailbox(
    client, monkeypatch, session, own_account, rolled_out
):
    revoked = []

    async def fake_revoke(grant_id):
        revoked.append(grant_id)

    monkeypatch.setattr(nylas, "revoke_grant", fake_revoke)

    resp = client.delete("/api/v1/accounts/me", headers=_headers())
    assert resp.json() == {"connected": False}
    assert revoked == ["grant-own-7"]
    assert client.get("/api/v1/accounts/me", headers=_headers()).json() == {
        "enabled": True, "connected": False,
    }


async def _bid(session, user_id=7):
    return await create_negotiation(
        session=session,
        company_id=1,
        dispatcher_user_id=user_id,
        load_uuid="load-uuid-1",
        bid_amount=3200,
    )


async def test_a_bid_leaves_from_the_dispatchers_own_mailbox_as_an_alert_reply(
    session, account, own_account, stubs, monkeypatch, load_snapshot  # noqa: F811
):
    load_snapshot.update(POSTING)
    sent = []

    async def never_search(grant_id, rfc_id):
        raise AssertionError("the personal mailbox never got the alert")

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "thread-own"}

    monkeypatch.setattr(nylas, "find_message_by_rfc_id", never_search)
    monkeypatch.setattr(nylas, "send_message", fake_send)

    negotiation = await _bid(session)

    assert sent[0]["grant_id"] == "grant-own-7"
    assert sent[0]["reply_to_message_id"] is None
    assert sent[0]["in_reply_to"] == POSTING["email_message_id"]
    assert sent[0]["subject"] == "Re: " + POSTING["email_subject"]
    assert negotiation.email_account_id == own_account.id


async def test_a_dispatcher_without_their_own_mailbox_uses_the_shared_one(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "t"}

    monkeypatch.setattr(nylas, "send_message", fake_send)

    negotiation = await _bid(session, user_id=8)

    assert sent[0]["grant_id"] == "grant-1"
    assert sent[0]["in_reply_to"] is None
    assert negotiation.email_account_id == account.id


async def test_a_dead_personal_mailbox_falls_back_to_the_shared_one(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    own_account.status = "expired"
    session.commit()
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "t"}

    monkeypatch.setattr(nylas, "send_message", fake_send)

    await _bid(session)

    assert sent[0]["grant_id"] == "grant-1"


def _pending_suggestion(session, negotiation):
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
    return suggestion


def test_a_reply_leaves_from_the_mailbox_that_sent_the_bid(
    client, session, account, own_account, negotiation, monkeypatch
):
    negotiation.email_account_id = own_account.id
    suggestion = _pending_suggestion(session, negotiation)
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"id": "msg-out-2", "thread_id": "thread-1"}

    monkeypatch.setattr(nylas, "send_message", fake_send)

    resp = client.post(
        f"/api/v1/suggestions/{suggestion.id}/send", headers=_headers(), json={}
    )
    assert resp.status_code == 200
    assert sent[0]["grant_id"] == "grant-own-7"


def test_a_reply_waits_when_the_bidding_mailbox_was_disconnected(
    client, session, account, own_account, negotiation, monkeypatch
):
    negotiation.email_account_id = own_account.id
    own_account.status = "revoked"
    suggestion = _pending_suggestion(session, negotiation)

    async def fake_send(**kwargs):
        raise AssertionError("must not answer from another address")

    monkeypatch.setattr(nylas, "send_message", fake_send)

    resp = client.post(
        f"/api/v1/suggestions/{suggestion.id}/send", headers=_headers(), json={}
    )
    assert resp.status_code == 409
    assert "jane@gmail.com" in str(resp.json())


def test_a_personal_inbox_does_not_adopt_a_shared_thread(
    session, account, own_account, negotiation
):
    message = {"id": "m", "thread_id": "thread-1", "subject": "Re: Bid"}
    assert inbound.match_negotiation(session, own_account, message, set()) is None
    assert inbound.match_negotiation(session, account, message, set()) == negotiation


def test_a_personal_thread_matches_only_in_its_own_inbox(
    session, account, own_account, negotiation
):
    negotiation.email_account_id = own_account.id
    session.commit()
    message = {"id": "m", "thread_id": "thread-1", "subject": "Re: Bid"}
    assert inbound.match_negotiation(session, own_account, message, set()) == negotiation
    assert inbound.match_negotiation(session, account, message, set()) is None


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {"data": {"id": "out-1"}}


async def test_the_alert_reply_headers_reach_nylas(monkeypatch):
    posted = []

    async def post(self, url, **kwargs):
        posted.append(kwargs["json"])
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    await nylas.send_message(
        grant_id="g", to_email="b@x.com", subject="Re: s", body="b",
        in_reply_to="87764f$oi77l@out07.sylectus.com",
    )

    assert posted[0]["custom_headers"] == [
        {"name": "In-Reply-To", "value": "<87764f$oi77l@out07.sylectus.com>"},
        {"name": "References", "value": "<87764f$oi77l@out07.sylectus.com>"},
    ]
    assert "reply_to_message_id" not in posted[0]


async def test_the_send_asks_nylas_for_the_headers_it_wrote(monkeypatch, caplog):
    calls = []

    async def post(self, url, **kwargs):
        calls.append(kwargs)
        r = _Response()
        r.json = lambda: {"data": {"id": "out-1", "headers": [
            {"name": "Message-Id", "value": "<out-1@mail.gmail.com>"},
            {"name": "In-Reply-To", "value": "<87764f$oi77l@out07.sylectus.com>"},
            {"name": "References", "value": "<87764f$oi77l@out07.sylectus.com>"},
        ]}}
        return r

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with caplog.at_level("INFO"):
        await nylas.send_message(
            grant_id="g", to_email="b@x.com", subject="Re: s", body="b",
            in_reply_to="87764f$oi77l@out07.sylectus.com",
        )

    assert calls[0]["params"] == {"fields": "include_basic_headers"}
    assert "as a reply: In-Reply-To <87764f$oi77l@out07.sylectus.com>" in caplog.text


async def test_a_reply_that_lost_its_headers_is_logged_as_an_error(monkeypatch, caplog):
    async def post(self, url, **kwargs):
        r = _Response()
        r.json = lambda: {"data": {"id": "out-1", "headers": [
            {"name": "Message-Id", "value": "<out-1@mail.gmail.com>"},
        ]}}
        return r

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with caplog.at_level("ERROR"):
        await nylas.send_message(
            grant_id="g", to_email="b@x.com", subject="Re: s", body="b",
            in_reply_to="<x@y>",
        )

    assert "without In-Reply-To" in caplog.text


async def test_a_reply_inside_the_mailbox_needs_no_custom_headers(monkeypatch):
    posted = []

    async def post(self, url, **kwargs):
        posted.append(kwargs["json"])
        return _Response()

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    await nylas.send_message(
        grant_id="g", to_email="b@x.com", subject="Re: s", body="b",
        reply_to_message_id="nylas-1", in_reply_to="<x@y>",
    )

    assert posted[0]["reply_to_message_id"] == "nylas-1"
    assert "custom_headers" not in posted[0]


async def test_a_reply_sent_from_the_dispatchers_own_gmail_lands_in_their_conversation(
    session, own_account, negotiation, monkeypatch
):
    from services import events

    negotiation.email_account_id = own_account.id
    session.commit()
    monkeypatch.setattr(events.hub, "publish", lambda e: None)
    message = {
        "id": "gmail-sent-1",
        "thread_id": "thread-1",
        "subject": "Re: Bid — Chicago, IL to Detroit, MI",
        "from": [{"email": "jane@gmail.com"}],
        "to": [{"email": "broker@acme-logistics.com"}],
        "body": "We can go 3100.",
    }
    await inbound.handle_own_send(session, own_account, message)
    session.commit()
    session.refresh(negotiation)
    (m,) = negotiation.messages
    assert (m.direction, m.from_email, m.body_text) == ("outbound", "jane@gmail.com", "We can go 3100.")
    assert m.sent_by_user_id is None  # shown as sent from a mail client
