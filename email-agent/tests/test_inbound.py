import hashlib
import hmac
import json

from datetime import datetime, timedelta, timezone

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


def test_strip_quoted_removes_outlook_vml_stylesheet():
    """
    The shape that reached dispatchers: Word-generated mail carries a <head> of
    VML behaviour rules, and flattening the tags left the stylesheet sitting on
    top of the broker's sentence.
    """
    body = (
        '<html xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office">'
        "<head>"
        '<meta name="Generator" content="Microsoft Word 15 (filtered medium)">'
        "<!--[if !mso]><style>v\\:* {behavior:url(#default#VML);}\n"
        "o\\:* {behavior:url(#default#VML);}\n"
        "w\\:* {behavior:url(#default#VML);}\n"
        ".shape {behavior:url(#default#VML);}\n"
        "</style><![endif]-->"
        "<style><!--\n/* Font Definitions */\n"
        '@font-face\n\t{font-family:"Cambria Math";\n\tpanose-1:2 4 5 3 5 4 6 3 2 4;}\n'
        "p.MsoNormal, li.MsoNormal\n\t{margin:0in;\n\tfont-size:11.0pt;}\n--></style>"
        "<!--[if gte mso 9]><xml>\n"
        '<o:shapedefaults v:ext="edit" spidmax="1026" />\n</xml><![endif]-->'
        "</head>"
        '<body lang="EN-US"><div class="WordSection1">'
        '<p class="MsoNormal">We can do $2,850 all in.<o:p></o:p></p>'
        "</div></body></html>"
    )
    assert inbound.strip_quoted(body) == "We can do $2,850 all in."


def test_strip_quoted_survives_an_unclosed_stylesheet():
    """A truncated <style> still must not leak CSS into the broker's words."""
    body = (
        "<html><head><style>\n"
        "v\\:* {behavior:url(#default#VML);}\n"
        ".shape {behavior:url(#default#VML);}\n"
        "<div>Rate works, send the ratecon.</div>"
    )
    cleaned = inbound.strip_quoted(body)
    assert cleaned == "Rate works, send the ratecon."
    assert "VML" not in cleaned


def test_strip_quoted_keeps_prose_containing_braces():
    """The CSS safety net must not eat a sentence that happens to use braces."""
    body = "<div>Use the template {name} on the BOL. Rate is $900.</div>"
    assert inbound.strip_quoted(body) == "Use the template {name} on the BOL. Rate is $900."


def test_strip_quoted_keeps_text_revealed_to_non_outlook_clients():
    """
    A downlevel-revealed conditional wraps real content in comment syntax.
    Stripping comments must not take the sentence between them with it.
    """
    body = (
        "<!--[if !mso]><!--><div>Confirmed at $3,100.</div><!--<![endif]-->"
        "<!--[if mso]><div>Outlook-only filler</div><![endif]-->"
    )
    assert inbound.strip_quoted(body) == "Confirmed at $3,100."


def test_strip_quoted_drops_html_quote_header_line():
    """Outlook's flattened "From:" header block is history, not a new message."""
    body = (
        "<div>Sounds good.</div>"
        "<div>From: Dispatch &lt;d@x.com&gt;<br>Sent: Monday<br>"
        "We offered $3,200.</div>"
    )
    cleaned = inbound.strip_quoted(body)
    assert cleaned == "Sounds good."
    assert "3,200" not in cleaned


def test_strip_quoted_leaves_no_ragged_indentation():
    """Each flattened line starts where the words do, not where a tag was."""
    body = "<div>Line one.</div><div>Line two.</div><div>&nbsp;</div><div>Line three.</div>"
    assert inbound.strip_quoted(body) == "Line one.\nLine two.\n\nLine three."


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


def test_normalize_subject_strips_stacked_reply_prefixes():
    assert inbound.normalize_subject("Re: Bid — Chicago") == "bid — chicago"
    assert inbound.normalize_subject("FWD: RE: Bid  —  Chicago") == "bid — chicago"
    assert inbound.normalize_subject("RE[2]: Bid — Chicago") == "bid — chicago"
    assert inbound.normalize_subject(None) == ""


async def test_a_reply_is_matched_when_the_thread_id_was_never_recorded(
    session, account, negotiation, captured, monkeypatch
):
    """
    The production shape: the send response carried no thread id, so every
    broker reply arrived on a thread we had never seen and was dropped. The
    negotiation showed our side of the conversation and nothing else.
    """
    negotiation.nylas_thread_id = None
    session.flush()
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "counter_offer", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": 2900, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "counter_offer", "draft_subject": "Re: Bid",
                     "draft_body": "We can do $3,000.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(
        session, account,
        make_message(
            thread_id="thread-nylas-never-told-us",
            subject="RE: Bid — Chicago, IL to Detroit, MI",
        ),
    )
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.direction == "inbound"
    assert stored.body_text == "We can do 2900, can you meet us there?"
    # Learned from the reply, so the next message matches on the thread alone.
    assert negotiation.nylas_thread_id == "thread-nylas-never-told-us"


async def test_a_reply_on_a_new_thread_is_matched_by_subject_and_broker(
    session, account, negotiation, captured, monkeypatch
):
    """Some broker systems answer on a thread of their own making."""
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "question", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": None, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "question", "draft_subject": "Re: Bid",
                     "draft_body": "Yes.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(
        session, account,
        make_message(thread_id="a-thread-of-their-own",
                     subject="Re: Bid — Chicago, IL to Detroit, MI"),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 1
    # The recorded thread is not overwritten: it is still the one we opened.
    assert negotiation.nylas_thread_id == "thread-1"


async def test_a_reply_from_a_colleague_at_the_broker_is_matched(
    session, account, negotiation, captured, monkeypatch
):
    """Brokerages answer from shared desks and covering agents."""
    monkeypatch.setattr(
        ai, "classify_inbound",
        lambda **k: {"intent": "question", "contains_ratecon": False,
                     "ratecon_attachment_name": None, "quoted_amount": None, "reasoning": ""},
    )
    monkeypatch.setattr(
        ai, "draft_reply",
        lambda **k: {"intent": "question", "draft_subject": "Re: Bid",
                     "draft_body": "Yes.", "reasoning": ""},
    )

    await inbound.handle_inbound_message(
        session, account,
        make_message(
            thread_id="unknown-thread",
            subject="Re: Bid — Chicago, IL to Detroit, MI",
            **{"from": [{"email": "dispatch@acme-logistics.com"}]},
        ),
    )
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.from_email == "dispatch@acme-logistics.com"


async def test_a_stranger_on_the_same_subject_is_not_matched(
    session, account, negotiation, captured, monkeypatch
):
    """Subject alone must never attach a message to someone else's load."""
    await inbound.handle_inbound_message(
        session, account,
        make_message(
            thread_id="unknown-thread",
            subject="Re: Bid — Chicago, IL to Detroit, MI",
            **{"from": [{"email": "someone@unrelated-brokerage.com"}]},
        ),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 0


async def test_a_lookalike_domain_is_not_treated_as_the_broker(
    session, account, load_snapshot, captured, monkeypatch
):
    """The domain is a LIKE pattern; its wildcards must not match literally."""
    n = models.Negotiation(
        company_id=1,
        load_uuid="load-uuid-9",
        load_snapshot=load_snapshot,
        bid_amount=3200.0,
        broker_email="broker@acmexlogistics.com",
        subject="Bid — Chicago, IL to Detroit, MI",
        status=models.BID_SENT,
    )
    session.add(n)
    session.flush()

    await inbound.handle_inbound_message(
        session, account,
        make_message(
            thread_id="unknown-thread",
            subject="Re: Bid — Chicago, IL to Detroit, MI",
            **{"from": [{"email": "someone@acme_logistics.com"}]},
        ),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 0


async def test_a_reply_on_a_different_subject_is_not_matched(
    session, account, negotiation, captured, monkeypatch
):
    await inbound.handle_inbound_message(
        session, account,
        make_message(thread_id="unknown-thread", subject="Re: A different load entirely"),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 0


async def test_subject_matching_does_not_reach_back_indefinitely(
    session, account, negotiation, captured, monkeypatch
):
    """A broker answering a months-old bid is not this negotiation's reply."""
    negotiation.nylas_thread_id = None
    negotiation.created_at = datetime.now(timezone.utc) - timedelta(
        days=inbound.MATCH_WINDOW_DAYS + 1
    )
    session.flush()

    await inbound.handle_inbound_message(
        session, account,
        make_message(thread_id="unknown-thread",
                     subject="Re: Bid — Chicago, IL to Detroit, MI"),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 0


async def test_an_out_of_app_reply_is_matched_without_a_thread_id(
    session, account, negotiation, captured, monkeypatch
):
    """A dispatcher answering from Gmail must land on the negotiation too."""
    negotiation.nylas_thread_id = None
    session.flush()

    await inbound.handle_own_send(
        session, account,
        make_own_send(thread_id="unknown-thread",
                      subject="Re: Bid — Chicago, IL to Detroit, MI"),
    )
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.direction == "outbound"
    assert stored.sent_by_user_id is None
    assert negotiation.nylas_thread_id == "unknown-thread"


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


# --- one email, more than one copy in the mailbox ---------------------------
#
# A reply written in Gmail or Outlook arrives as `message.created` twice when
# the client saves its own copy to Sent and the provider saves another. The
# Nylas ids differ, so the id alone never told them apart and the thread showed
# the reply twice.


MESSAGE_ID = "<CAF7y8mQ2vQ@mail.gmail.com>"


def with_headers(message, message_id=MESSAGE_ID):
    return dict(message, headers=[{"name": "Message-Id", "value": message_id}])


def test_rfc_message_id_reads_the_header_without_its_brackets():
    assert inbound.rfc_message_id(with_headers({})) == "CAF7y8mQ2vQ@mail.gmail.com"


def test_rfc_message_id_is_case_insensitive_about_the_header_name():
    message = {"headers": [{"name": "MESSAGE-ID", "value": " <abc@x> "}]}
    assert inbound.rfc_message_id(message) == "abc@x"


def test_rfc_message_id_is_empty_when_the_provider_returned_no_headers():
    assert inbound.rfc_message_id({"id": "msg-1"}) == ""
    assert inbound.rfc_message_id({"headers": []}) == ""
    assert inbound.rfc_message_id({"headers": [{"name": "Subject", "value": "Hi"}]}) == ""


async def test_two_mailbox_copies_of_one_reply_are_stored_once(
    session, account, negotiation, captured
):
    """The reported bug: one reply written in a mail client, shown twice."""
    await inbound.handle_own_send(
        session, account, with_headers(make_own_send(id="msg-client-copy"))
    )
    session.commit()
    await inbound.handle_own_send(
        session, account, with_headers(make_own_send(id="msg-provider-copy"))
    )
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.nylas_message_id == "msg-client-copy"
    assert stored.rfc_message_id == "CAF7y8mQ2vQ@mail.gmail.com"


async def test_two_copies_are_stored_once_when_no_headers_come_back(
    session, account, negotiation, captured
):
    """
    Not every provider returns headers. The same text, sent to the same
    negotiation seconds apart, is the second copy of one email.
    """
    await inbound.handle_own_send(session, account, make_own_send(id="copy-a"))
    session.commit()
    await inbound.handle_own_send(session, account, make_own_send(id="copy-b"))
    session.commit()

    assert session.query(models.EmailMessage).count() == 1


async def test_a_second_reply_saying_something_else_is_still_recorded(
    session, account, negotiation, captured
):
    await inbound.handle_own_send(session, account, make_own_send(id="reply-1"))
    session.commit()
    await inbound.handle_own_send(
        session, account,
        make_own_send(id="reply-2", body="<div>Actually, $3,050 and we are set.</div>"),
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 2


async def test_the_same_words_sent_again_much_later_are_recorded(
    session, account, negotiation, captured
):
    """
    The body check is how a duplicate is caught with no headers to go on, so
    it is held to a window. A dispatcher chasing a broker the next morning
    with the same sentence is writing a second email, not sending one twice.
    """
    await inbound.handle_own_send(session, account, make_own_send(id="chase-1"))
    session.commit()

    first = session.query(models.EmailMessage).one()
    first.created_at = datetime.now(timezone.utc) - timedelta(
        minutes=inbound.DUPLICATE_WINDOW_MINUTES + 1
    )
    session.commit()

    await inbound.handle_own_send(session, account, make_own_send(id="chase-2"))
    session.commit()

    assert session.query(models.EmailMessage).count() == 2


async def test_the_message_id_of_an_app_send_is_learned_from_its_echo(
    session, account, negotiation, captured
):
    """
    A send this app made is recorded from the send response, which carries no
    headers. Its webhook echo is where the Message-Id first appears, and
    keeping it is what recognises a second mailbox copy of that same email.
    """
    session.add(
        models.EmailMessage(
            negotiation_id=negotiation.id,
            nylas_message_id="msg-app-send",
            direction="outbound",
            from_email="dispatch@shipluxellc.com",
            subject="Re: Bid",
            body_text="Sent through the app.",
            sent_by_user_id=7,
        )
    )
    session.commit()

    await inbound.handle_own_send(
        session, account, with_headers(make_own_send(id="msg-app-send"))
    )
    session.commit()

    stored = session.query(models.EmailMessage).one()
    assert stored.sent_by_user_id == 7, "the app's own record is kept"
    assert stored.rfc_message_id == "CAF7y8mQ2vQ@mail.gmail.com"

    # The provider's second copy of that same send: a new Nylas id, the same
    # Message-Id, and text that no longer matches what the app stored.
    await inbound.handle_own_send(
        session, account, with_headers(make_own_send(id="msg-app-send-copy"))
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 1


async def test_two_copies_of_a_broker_reply_are_stored_once(
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

    await inbound.handle_inbound_message(
        session, account, with_headers(make_message(id="broker-copy-a"))
    )
    session.commit()
    await inbound.handle_inbound_message(
        session, account, with_headers(make_message(id="broker-copy-b"))
    )
    session.commit()

    assert session.query(models.EmailMessage).count() == 1
    assert session.query(models.Suggestion).count() == 1


async def test_two_identical_broker_messages_without_headers_are_both_kept(
    session, account, negotiation, captured, monkeypatch
):
    """
    The body check is deliberately outbound-only. Dropping a broker message
    costs a reply draft and the agreed rate read out of the thread, and a
    broker who writes "?" twice is talking to us twice.
    """
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

    await inbound.handle_inbound_message(session, account, make_message(id="nudge-1"))
    session.commit()
    await inbound.handle_inbound_message(session, account, make_message(id="nudge-2"))
    session.commit()

    assert session.query(models.EmailMessage).count() == 2


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
