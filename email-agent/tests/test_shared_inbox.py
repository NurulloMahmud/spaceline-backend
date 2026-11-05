"""
The shared mailbox read over IMAP: what a bid sent from a dispatcher's own
mailbox puts on the email so the broker's answer lands there, how the reader
turns a raw message into the pipeline's dict, and how that answer finds the
negotiation whichever mailbox sent the bid.
"""
import asyncio
from email.message import EmailMessage as MimeMessage

import pytest

from config.settings import config
from database import models
from services import inbound, shared_inbox
from services.boxtruck import BoxTruckError, boxtruck
from services import outbound
from tests.test_negotiation_service import POSTING, stubs  # noqa: F401
from tests.test_personal_mailbox import _bid, _headers, own_account  # noqa: F401

SHARED = "dispatch@shipluxellc.com"


@pytest.fixture
def reader_on(monkeypatch):
    monkeypatch.setattr(config, "SHARED_INBOX_ADDRESS", SHARED)
    monkeypatch.setattr(config, "SHARED_INBOX_APP_PASSWORD", "app-password")


def raw_email(
    *,
    sender="broker@acme-logistics.com",
    to="jane@gmail.com",
    cc=SHARED,
    subject="Re: Bid",
    text="We can do 3000.",
    html="<div>We can do 3000.</div>",
    message_id="<reply-1@acme>",
    in_reply_to="<bid-1@mail.gmail.com>",
    pdf=None,
) -> bytes:
    msg = MimeMessage()
    msg["From"] = f"Acme Broker <{sender}>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-Id"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["Date"] = "Wed, 05 Nov 2026 10:00:00 +0000"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="ratecon.pdf")
    return msg.as_bytes()


# --- what the send records ---------------------------------------------------


async def test_a_bid_records_the_message_id_it_went_out_with(
    session, account, own_account, stubs, monkeypatch, load_snapshot  # noqa: F811
):
    load_snapshot.update(POSTING)
    sent = []

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        sent.append(kwargs)
        return {
            "id": "msg-out-1",
            "thread_id": "thread-own",
            "headers": [{"name": "Message-Id", "value": "<bid-1@mail.gmail.com>"}],
        }
    monkeypatch.setattr(outbound, "send", fake_send)

    negotiation = await _bid(session)

    assert sent[0]["in_reply_to"] == POSTING["email_message_id"]
    stored = session.query(models.EmailMessage).filter_by(negotiation_id=negotiation.id).one()
    assert stored.rfc_message_id == "bid-1@mail.gmail.com"


def test_a_reply_to_a_message_read_over_imap_threads_on_its_message_id(
    client, session, account, own_account, negotiation, monkeypatch
):
    negotiation.email_account_id = own_account.id
    inbound_msg = models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id=None,
        rfc_message_id="reply-1@acme",
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
    sent = []

    async def fake_send(account, **kwargs):

        kwargs["grant_id"] = account.nylas_grant_id
        sent.append(kwargs)
        return {"id": "msg-out-2", "thread_id": "thread-1"}

    monkeypatch.setattr(outbound, "send", fake_send)

    resp = client.post(f"/api/v1/suggestions/{suggestion.id}/send", headers=_headers(), json={})
    assert resp.status_code == 200, resp.text
    assert sent[0]["in_reply_to"] == "reply-1@acme"


# --- parsing -----------------------------------------------------------------


def test_a_raw_message_becomes_the_pipelines_dict():
    message = shared_inbox.parse_message(
        raw_email(pdf=b"%PDF-1.4 fake"), "imap:1:5", "18c0ffee"
    )

    assert message["id"] == "imap:1:5"
    assert message["thread_id"] == "18c0ffee"
    assert message["from"] == [{"name": "Acme Broker", "email": "broker@acme-logistics.com"}]
    assert message["to"] == [{"name": "", "email": "jane@gmail.com"}]
    assert message["cc"] == [{"name": "", "email": SHARED}]
    assert message["subject"] == "Re: Bid"
    assert "<div>We can do 3000.</div>" in message["body"]
    assert message["snippet"] == "We can do 3000."
    assert message["headers"]["Message-Id"] == "<reply-1@acme>"
    assert message["headers"]["In-Reply-To"] == "<bid-1@mail.gmail.com>"
    assert inbound.rfc_message_id(message) == "reply-1@acme"
    assert inbound.referenced_ids(message) == {"bid-1@mail.gmail.com"}
    (attachment,) = message["attachments"]
    assert attachment["filename"] == "ratecon.pdf"
    assert attachment["content"] == b"%PDF-1.4 fake"
    assert attachment["size"] == len(b"%PDF-1.4 fake")


def test_a_plain_text_message_keeps_its_text():
    message = shared_inbox.parse_message(raw_email(html=None), None, None)
    assert message["body"].strip() == "We can do 3000."
    assert message["attachments"] == []


def test_referenced_ids_reads_both_header_shapes():
    nylas_shape = {"headers": [
        {"name": "In-Reply-To", "value": "<a@x>"},
        {"name": "References", "value": "<a@x> <b@x>"},
    ]}
    assert inbound.referenced_ids(nylas_shape) == {"a@x", "b@x"}
    assert inbound.referenced_ids({"headers": None}) == set()


# --- matching across mailboxes ----------------------------------------------


def _own_bid(session, negotiation, own_account):
    negotiation.email_account_id = own_account.id
    negotiation.nylas_thread_id = None
    session.add(models.EmailMessage(
        negotiation_id=negotiation.id,
        nylas_message_id="msg-out-1",
        rfc_message_id="bid-1@mail.gmail.com",
        direction="outbound",
        from_email=own_account.email_address,
        to_email=negotiation.broker_email,
        subject=negotiation.subject,
    ))
    session.commit()


def test_an_answer_in_the_shared_mailbox_finds_the_dispatchers_negotiation(
    session, account, own_account, negotiation
):
    _own_bid(session, negotiation, own_account)
    message = shared_inbox.parse_message(raw_email(subject="Re: something else"), None, "abc")

    # The shared mailbox did not send this bid, so on its own it may not claim
    # the thread; read as the catch-all it may.
    assert inbound.match_negotiation(session, account, message, {"broker@acme-logistics.com"}) is None
    found = inbound.match_negotiation(
        session, account, message, {"broker@acme-logistics.com"}, across_accounts=True
    )
    assert found == negotiation
    assert negotiation.nylas_thread_id == "abc"


def test_the_quoted_message_id_beats_the_subject(session, account, own_account, negotiation):
    """A broker whose client rewrites the subject still gets through."""
    _own_bid(session, negotiation, own_account)
    message = shared_inbox.parse_message(
        raw_email(subject="FW: load", in_reply_to="<bid-1@mail.gmail.com>"), None, None
    )
    assert inbound.match_negotiation(session, account, message, set(), across_accounts=True) == negotiation


# --- the reader ---------------------------------------------------------------


@pytest.fixture
def pipeline(monkeypatch):
    handled = []

    async def record(session, account, message, across_accounts=False):
        handled.append((account.email_address, message, across_accounts))

    monkeypatch.setattr(inbound, "handle_inbound_message", record)
    return handled


async def test_a_brokers_answer_goes_through_the_pipeline_as_the_catch_all(
    session, account, reader_on, pipeline
):
    await shared_inbox.handle(7, raw_email(), "abc", 1)

    ((mailbox, message, across_accounts),) = pipeline
    assert mailbox == SHARED
    assert across_accounts is True
    assert message["id"] is None
    assert message["thread_id"] == "abc"
    cursor = session.get(models.SharedInboxCursor, SHARED)
    assert (cursor.uidvalidity, cursor.last_uid) == (1, 7)
    assert session.query(models.ProcessedWebhook).filter_by(
        nylas_message_id=f"imap:{SHARED}:1:7"
    ).count() == 1


async def test_our_own_copy_of_a_bid_and_sylectus_alerts_are_skipped(
    session, account, own_account, reader_on, pipeline
):
    await shared_inbox.handle(8, raw_email(sender="jane@gmail.com"), None, 1)
    await shared_inbox.handle(9, raw_email(sender=SHARED), None, 1)
    await shared_inbox.handle(10, raw_email(sender="alerts@out01.sylectus.com"), None, 1)

    assert pipeline == []
    assert session.get(models.SharedInboxCursor, SHARED).last_uid == 10


async def test_a_message_handled_once_is_not_handled_again(session, account, reader_on, pipeline):
    await shared_inbox.handle(7, raw_email(), None, 1)
    await shared_inbox.handle(7, raw_email(), None, 1)
    assert len(pipeline) == 1


async def test_without_the_shared_account_the_message_waits(session, reader_on, pipeline):
    with pytest.raises(RuntimeError):
        await shared_inbox.handle(7, raw_email(), None, 1)
    assert pipeline == []
    assert session.get(models.SharedInboxCursor, SHARED) is None


async def test_the_first_pass_starts_at_the_end_of_the_folder(
    session, account, reader_on, pipeline, monkeypatch
):
    reads = []

    def read_batch(known, source):
        last_uid = known[1] if known else None
        reads.append(last_uid)
        return 42, 5776, [] if last_uid is None else [(5777, raw_email(), None)]

    monkeypatch.setattr(shared_inbox, "_read_batch", read_batch)

    assert await shared_inbox.poll_once({}) == 0
    assert reads == [None]
    assert session.get(models.SharedInboxCursor, SHARED).last_uid == 5776

    assert await shared_inbox.poll_once({}) == 1
    assert reads[-1] == 5776
    session.expire_all()
    assert session.get(models.SharedInboxCursor, SHARED).last_uid == 5777
    assert len(pipeline) == 1


async def test_renumbered_uids_restart_the_cursor(session, account, reader_on, pipeline, monkeypatch):
    session.add(models.SharedInboxCursor(mailbox=SHARED, uidvalidity=1, last_uid=100))
    session.commit()

    monkeypatch.setattr(shared_inbox, "_read_batch", lambda last_uid, source: (2, 10, []))
    assert await shared_inbox.poll_once({}) == 0
    session.expire_all()
    cursor = session.get(models.SharedInboxCursor, SHARED)
    assert (cursor.uidvalidity, cursor.last_uid) == (2, 10)


async def test_a_message_that_keeps_failing_is_skipped_after_a_few_passes(
    session, account, reader_on, monkeypatch, caplog
):
    session.add(models.SharedInboxCursor(mailbox=SHARED, uidvalidity=1, last_uid=6))
    session.commit()
    monkeypatch.setattr(
        shared_inbox, "_read_batch",
        lambda last_uid, source: (1, 8, [(7, raw_email(), None), (8, raw_email(message_id="<r2@acme>"), None)]),
    )
    seen = []

    async def boom(session, account, message, across_accounts=False):
        seen.append(message["headers"]["Message-Id"])
        if message["headers"]["Message-Id"] == "<reply-1@acme>":
            raise RuntimeError("classifier down")

    monkeypatch.setattr(inbound, "handle_inbound_message", boom)
    attempts = {}

    for _ in range(shared_inbox.MAX_ATTEMPTS - 1):
        assert await shared_inbox.poll_once(attempts) == 0
    # Still stuck on the first message, the second untouched.
    assert seen == ["<reply-1@acme>"] * (shared_inbox.MAX_ATTEMPTS - 1)
    session.expire_all()
    assert session.get(models.SharedInboxCursor, SHARED).last_uid == 6

    assert await shared_inbox.poll_once(attempts) == 1
    assert seen[-1] == "<r2@acme>"
    session.expire_all()
    assert session.get(models.SharedInboxCursor, SHARED).last_uid == 8
    assert attempts == {}
    assert "skipping it" in caplog.text


async def test_the_reader_says_when_the_shared_mailbox_is_off(monkeypatch, caplog):
    monkeypatch.setattr(config, "SHARED_INBOX_ADDRESS", "")

    async def stop(seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(shared_inbox.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await shared_inbox.poll_forever()
    assert "reader off" in caplog.text


# --- attachments read over IMAP ---------------------------------------------


async def test_a_ratecon_read_over_imap_needs_no_download(
    session, account, negotiation, monkeypatch
):
    parsed_with = []

    async def no_broker(**kwargs):
        return None

    async def parse(filename, content, broker_id=None):
        parsed_with.append((filename, content))
        raise BoxTruckError("unreadable")

    monkeypatch.setattr(boxtruck, "resolve_broker", no_broker)
    monkeypatch.setattr(boxtruck, "parse_ratecon", parse)

    row = models.EmailMessage(
        negotiation_id=negotiation.id, direction="inbound",
        from_email="broker@acme-logistics.com", body_text="Ratecon attached.",
        has_attachments=True,
    )
    session.add(row)
    session.flush()

    await inbound.handle_ratecon(
        session=session, account=account, negotiation=negotiation, message_row=row,
        attachments=[{"id": "x:0", "filename": "ratecon.pdf", "size": 13, "content": b"%PDF-1.4 fake"}],
        nylas_message_id=None,
    )
    assert parsed_with == [("ratecon.pdf", b"%PDF-1.4 fake")]
