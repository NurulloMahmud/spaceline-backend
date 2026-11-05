"""
A dispatcher's own mailbox: connected for them under Admin > Mailboxes, used
for their bids, and kept apart from the company's shared dispatch mailbox.
The bid replies to the Sylectus alert by Message-ID.
"""
import pytest

from config.settings import config
from database import models
from services import inbound
from services.negotiations import NegotiationError, create_negotiation
from services import outbound
from tests.test_auth import make_token
from tests.test_negotiation_service import POSTING, stubs  # noqa: F401


@pytest.fixture
def own_account(session):
    """Jane's own mailbox (user 7). Takes over the row bidder_mailbox may have
    made first, so the two fixtures agree whichever runs first."""
    acct = session.query(models.EmailAccount).filter_by(user_id=7).first()
    if acct is None:
        acct = models.EmailAccount(company_id=1, user_id=7, nylas_grant_id="grant-own-7", email_address="jane@gmail.com")
        session.add(acct)
    else:
        acct.nylas_grant_id = "grant-own-7"
        acct.email_address = "jane@gmail.com"
        acct.status = "active"
    session.commit()
    return acct


def _headers(**claims):
    return {"Authorization": f"Bearer {make_token(**claims)}"}


def test_company_status_reports_the_shared_mailbox_only(client, session, own_account):
    resp = client.get("/api/v1/accounts", headers=_headers())
    assert resp.json() == {"connected": False}


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
    # The bid replies to the alert by Message-ID, which Gmail threads for
    # SMTP-submitted mail whether or not the mailbox already holds the alert.
    load_snapshot.update(POSTING)
    sent = []

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        sent.append(kwargs)
        return {"id": "msg-out-1", "thread_id": "thread-own"}
    monkeypatch.setattr(outbound, "send", fake_send)

    negotiation = await _bid(session)

    assert sent[0]["grant_id"] == "grant-own-7"
    assert sent[0]["in_reply_to"] == POSTING["email_message_id"]
    assert sent[0]["subject"] == "Re: " + POSTING["email_subject"]
    assert negotiation.email_account_id == own_account.id


async def test_a_dispatcher_without_their_own_mailbox_cannot_bid_from_the_shared_one(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    """The shared mailbox never carries a new bid: the broker's answer must
    reach the person who bid, so the bid waits for an administrator."""
    async def never_send(account, **kwargs):
        raise AssertionError("nothing may be sent for a dispatcher without a mailbox")

    monkeypatch.setattr(outbound, "send", never_send)

    with pytest.raises(NegotiationError) as err:
        await _bid(session, user_id=8)

    assert err.value.status_code == 409
    assert err.value.code == "mailbox_not_connected"
    assert "Admin > Mailboxes" in err.value.message


async def test_a_dead_personal_mailbox_does_not_fall_back_to_the_shared_one(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    own_account.status = "expired"
    session.commit()

    async def never_send(account, **kwargs):
        raise AssertionError("nothing may be sent from a dead mailbox")

    monkeypatch.setattr(outbound, "send", never_send)

    with pytest.raises(NegotiationError) as err:
        await _bid(session)

    assert err.value.code == "mailbox_not_connected"
    assert "jane@gmail.com" in err.value.message


async def test_a_dispatcher_without_a_mailbox_cannot_bid(
    session, account, stubs, monkeypatch  # noqa: F811
):
    """A bid waits until an administrator has connected the dispatcher's
    mailbox; the company's shared mailbox never stands in."""
    session.query(models.EmailAccount).filter_by(user_id=7).delete()
    session.commit()

    async def never_send(account, **kwargs):
        kwargs["grant_id"] = account.nylas_grant_id
        raise AssertionError("nothing may be sent without the dispatcher's mailbox")

    monkeypatch.setattr(outbound, "send", never_send)

    with pytest.raises(NegotiationError) as err:
        await _bid(session)

    assert err.value.code == "mailbox_not_connected"
    assert err.value.status_code == 409
    assert "Admin > Mailboxes" in err.value.message
    assert session.query(models.Negotiation).count() == 0


async def test_a_dispatcher_with_a_dead_mailbox_is_told_to_reconnect(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    own_account.status = "expired"
    session.commit()

    async def never_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        raise AssertionError("nothing may be sent from the shared mailbox")

    monkeypatch.setattr(outbound, "send", never_send)

    with pytest.raises(NegotiationError) as err:
        await _bid(session)

    assert err.value.code == "mailbox_not_connected"
    assert own_account.email_address in err.value.message


async def test_a_dispatcher_who_disconnected_is_asked_to_connect_again(
    session, account, own_account, stubs, monkeypatch  # noqa: F811
):
    own_account.status = "revoked"
    session.commit()

    with pytest.raises(NegotiationError) as err:
        await _bid(session)

    assert "Admin > Mailboxes" in err.value.message


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

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        sent.append(kwargs)
        return {"id": "msg-out-2", "thread_id": "thread-1"}

    monkeypatch.setattr(outbound, "send", fake_send)

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

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        raise AssertionError("must not answer from another address")

    monkeypatch.setattr(outbound, "send", fake_send)

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
