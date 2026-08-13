import hashlib
import hmac
import json

import pytest

from database import models
from services import events, inbound
from services.ai import ai
from services.nylas_client import nylas


def test_strip_quoted_removes_history():
    body = (
        "Can you do 3000?\n\n"
        "From: dispatch@shipluxellc.com\n"
        "Sent: Monday\n"
        "> RATE: $3,200\n"
        "> MC: 846834"
    )
    cleaned = inbound.strip_quoted(body)
    assert cleaned == "Can you do 3000?"


def test_strip_quoted_flattens_html():
    assert inbound.strip_quoted("<p>Hello&nbsp;there</p>").strip() == "Hello there"


def test_strip_quoted_handles_empty():
    assert inbound.strip_quoted("") == ""


def test_strip_quoted_removes_gmail_html_quote():
    """
    The shape that reached production: the reply is two words, and everything
    after it is our own previous email. Flattening the tags first left the
    quoted price in the text the model read.
    """
    body = (
        '<div dir="ltr">No, sorry.</div><br>'
        '<div class="gmail_quote gmail_quote_container">'
        '<div dir="ltr" class="gmail_attr">On Tue, 4 Aug 2026 at 17:21, '
        "J &amp; J WASATCH Dispatch &lt;dispatch@jjwasatchlogistics.com&gt; wrote:<br></div>"
        '<blockquote class="gmail_quote">Thank you for your offer. We need to '
        "hold at $200 for this load.</blockquote></div>"
    )
    cleaned = inbound.strip_quoted(body)
    assert cleaned == "No, sorry."
    assert "$200" not in cleaned


def test_strip_quoted_removes_plaintext_attribution():
    body = (
        "We can do $2,900.\n\n"
        "On Tue, 4 Aug 2026 at 17:21, Dispatch <d@x.com> wrote:\n"
        "> Our rate is $3,200."
    )
    cleaned = inbound.strip_quoted(body)
    assert cleaned == "We can do $2,900."
    assert "3,200" not in cleaned


def test_strip_quoted_removes_outlook_quote():
    body = (
        "<div>Sounds good, sending ratecon.</div>"
        '<div id="appendonsend"></div><hr id="stopSpelling">'
        '<div id="divRplyFwdMsg">From: Dispatch<br>We offered $3,200.</div>'
    )
    assert inbound.strip_quoted(body) == "Sounds good, sending ratecon."


def test_strip_quoted_decodes_entities():
    body = "<div>Rate is $1,500 &amp; we need a liftgate &lt;urgent&gt;</div>"
    assert inbound.strip_quoted(body) == "Rate is $1,500 & we need a liftgate <urgent>"


def test_strip_quoted_keeps_prose_mentioning_wrote():
    """The attribution pattern must not eat an ordinary sentence."""
    body = "<div>Our broker wrote the BOL already. Rate is $900.</div>"
    assert inbound.strip_quoted(body) == "Our broker wrote the BOL already. Rate is $900."
    assert inbound.strip_quoted(None) == ""


def test_pdf_attachments_only():
    message = {
        "attachments": [
            {"id": "1", "filename": "ratecon.pdf", "content_type": "application/pdf"},
            {"id": "2", "filename": "logo.png", "content_type": "image/png"},
            {"id": "3", "filename": "confirm.PDF", "content_type": "application/octet-stream"},
        ]
    }
    ids = [a["id"] for a in inbound.pdf_attachments(message)]
    assert ids == ["1", "3"]


def test_webhook_signature_is_enforced():
    body = json.dumps({"type": "message.created"}).encode()
    good = hmac.new(b"test-webhook-secret", body, hashlib.sha256).hexdigest()

    assert nylas.verify_webhook(good, body) is True
    assert nylas.verify_webhook("deadbeef", body) is False
    assert nylas.verify_webhook("", body) is False
    assert nylas.verify_webhook(good, b'{"type":"tampered"}') is False


@pytest.fixture
def captured(monkeypatch):
    published = []
    monkeypatch.setattr(events.hub, "publish", lambda e: published.append(e))
    return published


def make_message(**overrides):
    message = {
        "id": "msg-in-1",
        "thread_id": "thread-1",
        "subject": "Re: Bid",
        "body": "We can do 2900, can you meet us there?",
        "from": [{"email": "broker@acme-logistics.com"}],
        "attachments": [],
    }
    message.update(overrides)
    return message


async def test_broker_reply_creates_a_suggestion(
    session, account, negotiation, captured, monkeypatch
):
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "counter_offer", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": 2900, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "counter_offer", "draft_subject": "Re: Bid",
                     "draft_body": "We are holding at $3,200.", "reasoning": "Hold the rate."},
    )

    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()
    session.refresh(negotiation)

    assert negotiation.status == models.NEGOTIATING

    stored = session.query(models.EmailMessage).one()
    assert stored.direction == "inbound"
    assert stored.from_email == "broker@acme-logistics.com"

    suggestion = session.query(models.Suggestion).one()
    assert suggestion.kind == models.KIND_REPLY
    assert suggestion.status == models.PENDING
    assert suggestion.draft_body == "We are holding at $3,200."
    assert suggestion.in_reply_to_message_id == stored.id

    assert [e.type for e in captured] == [events.SUGGESTION_CREATED]


async def test_a_failed_draft_still_reaches_the_dispatcher(
    session, account, negotiation, captured, monkeypatch
):
    """If the model cannot draft, the dispatcher is still told a reply arrived."""
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "question", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": None, "reasoning": ""},
    )
    monkeypatch.setattr(ai, "draft_reply", lambda **k: None)

    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()

    suggestion = session.query(models.Suggestion).one()
    assert suggestion.status == models.PENDING
    assert "could not draft" in suggestion.ai_reasoning
    assert [e.type for e in captured] == [events.SUGGESTION_CREATED]


async def test_classification_failure_falls_back_to_a_reply_draft(
    session, account, negotiation, captured, monkeypatch
):
    """An unclassifiable message must never be silently treated as a ratecon."""
    monkeypatch.setattr(ai, "classify_inbound", lambda **k: None)
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "other", "draft_subject": "Re: Bid",
                     "draft_body": "Thanks — following up.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(
        session, account, make_message(attachments=[{"id": "a", "filename": "x.pdf"}])
    )
    session.commit()

    assert session.query(models.RateconCheck).count() == 0
    assert session.query(models.Suggestion).one().kind == models.KIND_REPLY


async def test_messages_on_unknown_threads_are_ignored(
    session, account, negotiation, captured, monkeypatch
):
    await inbound.handle_inbound_message(
        session, account, make_message(thread_id="some-other-thread")
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 0
    assert session.query(models.Suggestion).count() == 0
    assert captured == []


async def test_a_duplicate_delivery_is_stored_once(
    session, account, negotiation, captured, monkeypatch
):
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "question", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": None, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "question", "draft_subject": "Re: Bid",
                     "draft_body": "Reply.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()
    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()

    assert session.query(models.EmailMessage).count() == 1
    assert session.query(models.Suggestion).count() == 1


async def test_a_booked_negotiation_stops_drafting_replies(
    session, account, negotiation, captured, monkeypatch
):
    negotiation.status = models.BOOKED
    session.commit()

    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()

    assert session.query(models.EmailMessage).count() == 1, "the message is still archived"
    assert session.query(models.Suggestion).count() == 0
    assert captured == []


async def test_thread_text_labels_both_sides(session, negotiation):
    session.add_all([
        models.EmailMessage(
            negotiation_id=negotiation.id, direction="outbound", body_text="RATE: $3,200"
        ),
        models.EmailMessage(
            negotiation_id=negotiation.id, direction="inbound", body_text="Can you do 3000?"
        ),
    ])
    session.commit()
    session.refresh(negotiation)

    text = inbound.thread_text(negotiation)
    assert "CARRIER (us)" in text and "RATE: $3,200" in text
    assert "BROKER" in text and "Can you do 3000?" in text


async def test_negotiation_context_hides_the_driver_amount(session, negotiation):
    context = inbound.negotiation_context(negotiation)
    assert "3,200" in context
    assert "2400" not in context and "2,400" not in context


# --- replies sent from outside the app -------------------------------------


def make_own_send(**overrides):
    """A reply the dispatch mailbox sent that did not go through this app."""
    message = {
        "id": "msg-out-of-app-1",
        "thread_id": "thread-1",
        "subject": "Re: Bid",
        "body": "<div>We can come down to $3,000 on this one.</div>",
        "from": [{"email": "dispatch@shipluxellc.com"}],
        "to": [{"email": "broker@acme-logistics.com"}],
        "attachments": [],
    }
    message.update(overrides)
    return message


async def test_reply_sent_from_gmail_is_recorded_on_the_thread(
    session, account, negotiation, captured
):
    """
    A dispatcher answering the broker from their own mail client must still
    land on the negotiation, or the agent reads an incomplete conversation.
    """
    await inbound.handle_own_send(session, account, make_own_send())
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.direction == "outbound"
    assert stored.from_email == "dispatch@shipluxellc.com"
    assert stored.to_email == "broker@acme-logistics.com"
    assert "3,000" in stored.body_text
    # Null sender is what marks it as sent outside the app.
    assert stored.sent_by_user_id is None

    # It is us, so nothing is classified and no reply is drafted.
    assert session.query(models.Suggestion).count() == 0


async def test_out_of_app_reply_reaches_the_ai_as_our_own_words(
    session, account, negotiation
):
    """
    The rate agreed out of band has to be visible to extract_agreed_rate, or a
    correctly-priced rate confirmation gets rejected as a mismatch.
    """
    await inbound.handle_own_send(session, account, make_own_send())
    session.commit()
    session.refresh(negotiation)

    history = inbound.thread_text(negotiation)
    assert "CARRIER (us)" in history
    assert "$3,000" in history
    assert "BROKER" not in history


async def test_app_sent_message_is_not_stored_twice(session, account, negotiation):
    """The webhook echoes our own sends back; they are already recorded."""
    session.add(
        models.EmailMessage(
            negotiation_id=negotiation.id,
            nylas_message_id="msg-out-of-app-1",
            direction="outbound",
            from_email="dispatch@shipluxellc.com",
            subject="Re: Bid",
            body_text="Sent through the app.",
            sent_by_user_id=7,
        )
    )
    session.commit()

    await inbound.handle_own_send(session, account, make_own_send())
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.sent_by_user_id == 7
    assert stored.body_text == "Sent through the app."


async def test_own_send_on_an_unknown_thread_is_ignored(session, account, negotiation):
    """Ordinary mail from the shared mailbox is not a negotiation."""
    await inbound.handle_own_send(
        session, account, make_own_send(thread_id="some-other-thread")
    )
    session.commit()
    assert session.query(models.EmailMessage).count() == 0


# --- replies stay on the broker's original email chain ----------------------


def test_reply_subject_keeps_the_thread(negotiation):
    from services.negotiations import reply_subject

    negotiation.subject = "Bid — Wood Dale, IL 60191 to Dover, PA 17315 (Ref 2202907)"
    assert reply_subject(negotiation) == (
        "Re: Bid — Wood Dale, IL 60191 to Dover, PA 17315 (Ref 2202907)"
    )


def test_reply_subject_does_not_stack_re_prefixes(negotiation):
    from services.negotiations import reply_subject

    negotiation.subject = "RE: re: Bid — Chicago to Detroit"
    assert reply_subject(negotiation) == "Re: Bid — Chicago to Detroit"


async def test_drafted_reply_never_renames_the_thread(
    session, account, negotiation, captured, monkeypatch
):
    """
    The model used to name its own drafts, which showed up in the broker's
    inbox as a brand new email chain.
    """
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "counter_offer", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": None, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "counter_offer",
                     "draft_subject": "Rate Confirmation Needed for Wood Dale to Dover",
                     "draft_body": "Holding at $3,200.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(session, account, make_message())
    session.commit()

    suggestion = session.query(models.Suggestion).one()
    assert suggestion.draft_subject == f"Re: {negotiation.subject}"
    assert "Rate Confirmation Needed" not in suggestion.draft_subject
