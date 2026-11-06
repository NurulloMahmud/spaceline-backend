"""Sending that survives a busy server, one standing IMAP connection per
mailbox, and the alerts that say when a mailbox or the reader is in trouble."""
import imaplib
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from database import models
from services import alerts, imap_smtp, outbound, shared_inbox
from tests.test_mailboxes import admin_on, imap_account  # noqa: F401


async def drain():
    """Run the tasks a test left behind (sleep is stubbed, so it cannot yield)."""
    import asyncio
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


@pytest.fixture
def no_wait(monkeypatch):
    waited = []

    async def sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr(outbound.asyncio, "sleep", sleep)
    return waited


@pytest.fixture
def said(monkeypatch):
    texts = []

    async def notify(text):
        texts.append(text)
        return True

    monkeypatch.setattr(alerts, "notify", notify)
    alerts._standing.clear()
    alerts._failing_since.clear()
    alerts.last_reader_pass = None
    return texts


# --- SMTP retries -------------------------------------------------------------

def test_which_failures_are_worth_another_try():
    assert imap_smtp.transient(smtplib.SMTPResponseException(421, b"4.4.5 Server busy, try again later"))
    assert imap_smtp.transient(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    assert imap_smtp.transient(TimeoutError("timed out"))
    assert imap_smtp.transient(smtplib.SMTPRecipientsRefused({"a@b.c": (450, b"try later")}))
    assert not imap_smtp.transient(smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"))
    assert not imap_smtp.transient(smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"no such user")}))
    assert not imap_smtp.transient(smtplib.SMTPDataError(552, b"too big"))
    assert imap_smtp.sign_in_refused(imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials"))
    assert not imap_smtp.sign_in_refused(imaplib.IMAP4.abort("socket error: EOF"))


async def test_a_busy_server_is_tried_again_and_the_bid_goes_out(session, imap_account, monkeypatch, no_wait, said):
    calls = []

    def flaky(creds, msg):
        calls.append(creds.address)
        if len(calls) < 3:
            raise smtplib.SMTPResponseException(421, b"4.4.5 Server busy, try again later")
        return "m1@spaceline23llc.com"

    monkeypatch.setattr(imap_smtp, "send", flaky)
    sent = await outbound.send(imap_account, to_email="broker@acme.com", subject="Re: load", body="<p>hi</p>")
    assert outbound.sent_rfc_id(sent) == "m1@spaceline23llc.com"
    assert len(calls) == 3
    assert no_wait == [2, 5]
    assert said == []
    session.expire_all()
    assert imap_account.status == "active"


async def test_a_server_that_stays_busy_fails_after_the_last_try_and_alerts(session, imap_account, monkeypatch, no_wait, said):
    def busy(creds, msg):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    monkeypatch.setattr(imap_smtp, "send", busy)
    with pytest.raises(outbound.SendError) as e:
        await outbound.send(imap_account, to_email="broker@acme.com", subject="Re: load", body="<p>hi</p>")
    assert "could not send over SMTP" in str(e.value)
    assert no_wait == list(outbound.RETRY_DELAYS)
    await drain()  # the alert is a fire-and-forget task
    assert len(said) == 1 and "after 4 tries" in said[0] and imap_account.email_address in said[0]
    session.expire_all()
    assert imap_account.status == "active"

    # a second failure inside the window does not alert again
    with pytest.raises(outbound.SendError):
        await outbound.send(imap_account, to_email="broker@acme.com", subject="Re: load", body="<p>hi</p>")
    await drain()
    assert len(said) == 1


async def test_a_refused_password_fails_at_once_and_marks_the_mailbox(session, imap_account, monkeypatch, no_wait, said):
    def refused(creds, msg):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(imap_smtp, "send", refused)
    with pytest.raises(outbound.SendError):
        await outbound.send(imap_account, to_email="broker@acme.com", subject="Re: load", body="<p>hi</p>")
    assert no_wait == []
    session.expire_all()
    assert imap_account.status == "failing"
    assert imap_account.last_error.startswith("SMTP:")


# --- one standing IMAP connection per mailbox --------------------------------

class FakeBox:
    opened = []
    closed = []
    refreshed = []
    fail_next = None  # an exception the next command raises

    def __init__(self, host, user, password, folder, port=993):
        self.user = user
        self.uidvalidity = 7
        self.counted = 0

    def open(self):
        FakeBox.opened.append(self.user)
        return self

    def close(self):
        FakeBox.closed.append(self.user)

    def _maybe_fail(self):
        if FakeBox.fail_next is not None:
            e, FakeBox.fail_next = FakeBox.fail_next, None
            raise e

    def last_uid(self):
        self._maybe_fail()
        self.counted += 1
        return 100

    def refresh(self):
        self._maybe_fail()
        FakeBox.refreshed.append(self.user)

    def uids_after(self, last):
        return [last + 1]

    def fetch(self, uid):
        return b"raw", None


@pytest.fixture
def boxes(monkeypatch):
    FakeBox.opened.clear()
    FakeBox.closed.clear()
    FakeBox.refreshed.clear()
    FakeBox.fail_next = None
    shared_inbox._connections.clear()
    monkeypatch.setattr(shared_inbox, "Mailbox", FakeBox)
    yield FakeBox
    shared_inbox._connections.clear()


def source(address="royj@spaceline23llc.com"):
    return shared_inbox.Source(address=address, host="imap.gmail.com", port=993, password="x", folder="INBOX", account_id=None, across_accounts=False)


def test_the_connection_is_opened_once_and_the_folder_counted_only_at_the_start(boxes):
    src = source()
    assert shared_inbox._read_batch(None, src) == (7, 100, [])
    assert shared_inbox._read_batch((7, 100), src) == (7, 100, [(101, b"raw", None)])
    assert shared_inbox._read_batch((7, 101), src) == (7, 101, [(102, b"raw", None)])
    assert boxes.opened == ["royj@spaceline23llc.com"]
    assert shared_inbox._connections[src.key].counted == 1


def test_every_pass_on_a_kept_connection_asks_for_new_mail_first(boxes):
    # Gmail shows a kept-open session no new mail until it is asked (NOOP);
    # without it mail waited until Gmail dropped the connection.
    src = source()
    shared_inbox._read_batch(None, src)
    assert boxes.refreshed == []          # the first pass only records the end
    shared_inbox._read_batch((7, 100), src)
    shared_inbox._read_batch((7, 101), src)
    assert boxes.refreshed == ["royj@spaceline23llc.com"] * 2


def test_a_dropped_connection_is_replaced_within_the_same_pass(boxes):
    src = source()
    shared_inbox._read_batch(None, src)
    boxes.fail_next = imaplib.IMAP4.abort("socket error: EOF")
    assert shared_inbox._read_batch((7, 100), src) == (7, 100, [(101, b"raw", None)])
    assert boxes.opened == ["royj@spaceline23llc.com"] * 2
    assert boxes.closed == ["royj@spaceline23llc.com"]


def test_a_mailbox_that_cannot_be_opened_fresh_raises_and_keeps_nothing(boxes):
    src = source()
    boxes.fail_next = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
    with pytest.raises(imaplib.IMAP4.error):
        shared_inbox._read_batch(None, src)
    assert src.key not in shared_inbox._connections


def test_mailboxes_no_longer_read_are_signed_out(boxes):
    shared_inbox._read_batch(None, source("a@spaceline23llc.com"))
    shared_inbox._read_batch(None, source("b@spaceline23llc.com"))
    shared_inbox.close_unused({"a@spaceline23llc.com"})
    assert list(shared_inbox._connections) == ["a@spaceline23llc.com"]
    assert boxes.closed == ["b@spaceline23llc.com"]
    shared_inbox.close_all()
    assert shared_inbox._connections == {}


# --- the watchdog --------------------------------------------------------------

async def test_a_refused_password_is_said_once_repeated_hourly_and_cleared(session, imap_account, said):
    now = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
    imap_account.status = "failing"
    imap_account.last_error = "IMAP: [AUTHENTICATIONFAILED] Invalid credentials"
    session.commit()
    assert len(await alerts.check(session, now)) == 1
    assert said[0].startswith("ALERT: mailbox alex@spaceline23llc.com (user 4): password refused")
    assert await alerts.check(session, now + timedelta(minutes=30)) == []
    assert len(await alerts.check(session, now + timedelta(minutes=61))) == 1
    assert said[-1].startswith("STILL:")
    imap_account.status = "active"
    imap_account.last_error = None
    session.commit()
    assert len(await alerts.check(session, now + timedelta(minutes=62))) == 1
    assert said[-1] == "CLEARED: mailbox alex@spaceline23llc.com (user 4) signs in again."
    assert await alerts.check(session, now + timedelta(minutes=63)) == []


async def test_reads_failing_for_a_while_are_reported_but_a_single_slow_pass_is_not(session, imap_account, said):
    now = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
    imap_account.last_error = "IMAP, last pass: The read operation timed out; trying again"
    session.commit()
    assert await alerts.check(session, now) == []
    assert await alerts.check(session, now + timedelta(minutes=5)) == []
    out = await alerts.check(session, now + timedelta(minutes=10))
    assert len(out) == 1 and "inbox reads have failed for 10 minutes" in out[0]
    imap_account.last_error = None
    session.commit()
    out = await alerts.check(session, now + timedelta(minutes=11))
    assert out == ["mailbox alex@spaceline23llc.com (user 4): inbox reads are back."]


async def test_a_quiet_reader_is_reported(session, said):
    now = datetime(2026, 11, 5, 16, 0, tzinfo=timezone.utc)
    alerts.last_reader_pass = now - timedelta(minutes=2)
    assert await alerts.check(session, now) == []
    alerts.last_reader_pass = now - timedelta(minutes=6)
    out = await alerts.check(session, now)
    assert len(out) == 1 and "has not finished a pass for 6 minutes" in out[0]
    alerts.last_reader_pass = now
    assert await alerts.check(session, now + timedelta(seconds=1)) == ["the inbox reader is finishing passes again."]


async def test_without_an_alert_chat_the_alert_goes_to_the_log(monkeypatch, caplog):
    monkeypatch.setattr(alerts.config, "ALERT_CHAT_ID", "")
    assert await alerts.notify("something") is False
    assert "no ALERT_CHAT_ID" in caplog.text
