"""
Mailboxes connected with an app password: who may enter them, what is
stored, how such a mailbox sends and is read, and that Nylas mailboxes are
left exactly as they were.
"""
from email import message_from_bytes
from email import policy

import pytest
from cryptography.fernet import Fernet

from config.settings import config
from database import models
from services import imap_smtp, outbound, secrets, shared_inbox
from services.mailboxes import catcher_headers
from services.negotiations import find_posting_email
from services.nylas_client import nylas
from tests.test_auth import make_token
from tests.test_negotiation_service import POSTING, stubs  # noqa: F401
from tests.test_personal_mailbox import _bid, own_account  # noqa: F401

ADMIN = 9


@pytest.fixture
def admin_on(monkeypatch):
    monkeypatch.setattr(config, "MAILBOX_ADMIN_USERS", str(ADMIN))
    monkeypatch.setattr(config, "MAILBOX_SECRET_KEY", Fernet.generate_key().decode())


def _h(user_id=ADMIN, **claims):
    return {"Authorization": f"Bearer {make_token(user_id=user_id, **claims)}"}


CREDS = {
    "email_address": "alex@spaceline23llc.com",
    "password": "abcd efgh ijkl mnop",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
}


@pytest.fixture
def signs_in(monkeypatch):
    """The provider accepts whatever is typed; what was tried is recorded."""
    tried = []

    def check(creds, smtp=True):
        tried.append(creds)
        return imap_smtp.CheckResult(imap_ok=True, inbox_messages=1204, smtp_ok=True, alerts_today=38)

    monkeypatch.setattr(imap_smtp, "check", check)
    return tried


@pytest.fixture
def refuses(monkeypatch):
    def check(creds, smtp=True):
        return imap_smtp.CheckResult(imap_ok=False, imap_error="[AUTHENTICATIONFAILED] Invalid credentials", smtp_ok=True)

    monkeypatch.setattr(imap_smtp, "check", check)


# --- who gets in -------------------------------------------------------------


def test_the_page_is_open_to_management_developers_and_the_allowlist(client, admin_on):
    assert client.get("/api/v1/mailboxes", headers=_h(user_id=7, department="Dispatch")).status_code == 403
    assert client.put("/api/v1/mailboxes/users/4", headers=_h(user_id=7, department="Billing"), json=CREDS).status_code == 403
    assert client.get("/api/v1/mailboxes", headers=_h(user_id=7, department="Management")).status_code == 200
    assert client.get("/api/v1/mailboxes", headers=_h(user_id=8, department="Developer")).status_code == 200
    assert client.get("/api/v1/mailboxes", headers=_h()).status_code == 200


def test_the_allowlist_alone_still_opens_it(client, monkeypatch):
    monkeypatch.setattr(config, "MAILBOX_ADMIN_USERS", "")
    assert client.get("/api/v1/mailboxes", headers=_h()).status_code == 403
    monkeypatch.setattr(config, "MAILBOX_ADMIN_USERS", str(ADMIN))
    assert client.get("/api/v1/mailboxes", headers=_h()).status_code == 200


# --- the posted-loads listener mailbox ------------------------------------------


LISTENER = {
    "email_address": "spaceline@boxtruckmanage.com",
    "password": "wxyz wxyz wxyz wxyz",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "folder": "Posted loads",
    "imap_only": True,
}


def test_only_a_developer_sets_the_listener_mailbox(client, admin_on, signs_in):
    assert client.put("/api/v1/mailboxes/listener", headers=_h(department="Management"), json=LISTENER).status_code == 403
    resp = client.put("/api/v1/mailboxes/listener", headers=_h(department="Developer"), json=LISTENER)
    assert resp.status_code == 200, resp.text
    assert resp.json()["listener"]["folder"] == "Posted loads"
    assert signs_in[0].folder == "Posted loads"
    assert signs_in[0].smtp_host == ""


def test_the_listener_mailbox_is_stored_encrypted_and_listed(client, session, account, admin_on, signs_in):
    client.put("/api/v1/mailboxes/listener", headers=_h(department="Developer"), json=LISTENER)
    row = session.query(models.ListenerMailbox).one()
    assert row.secret != LISTENER["password"]
    assert secrets.decrypt(row.secret) == LISTENER["password"]
    assert row.updated_by_user_id == ADMIN

    listed = client.get("/api/v1/mailboxes", headers=_h(department="Management")).json()
    assert listed["listener"]["email_address"] == "spaceline@boxtruckmanage.com"
    assert listed["can_manage_listener"] is False
    assert client.get("/api/v1/mailboxes", headers=_h(department="Developer")).json()["can_manage_listener"] is True
    assert LISTENER["password"] not in str(listed)


def test_the_listener_mailbox_can_be_edited_without_retyping_the_password(client, session, admin_on, signs_in):
    client.put("/api/v1/mailboxes/listener", headers=_h(department="Developer"), json=LISTENER)
    resp = client.put(
        "/api/v1/mailboxes/listener", headers=_h(department="Developer"),
        json=dict(LISTENER, password=None, folder="Alerts"),
    )
    assert resp.status_code == 200, resp.text
    assert signs_in[-1].password == LISTENER["password"]
    assert session.query(models.ListenerMailbox).one().folder == "Alerts"


def test_a_listener_mailbox_that_does_not_sign_in_is_not_stored(client, session, admin_on, refuses):
    resp = client.put("/api/v1/mailboxes/listener", headers=_h(department="Developer"), json=LISTENER)
    assert resp.status_code == 422
    assert session.query(models.ListenerMailbox).count() == 0


def test_an_imap_only_check_skips_smtp(monkeypatch):
    calls = []

    class FakeIMAP:
        def __init__(self, host, port, timeout=None):
            calls.append(("imap", host, port))

        def login(self, user, password):
            pass

        def select(self, folder, readonly=True):
            return "OK", [b"12"]

        def uid(self, *args):
            return "OK", [b"1 2 3"]

        def logout(self):
            pass

    def no_smtp(creds):
        raise AssertionError("SMTP must not be tried")

    monkeypatch.setattr(imap_smtp.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(imap_smtp, "_smtp", no_smtp)
    result = imap_smtp.check(
        imap_smtp.Credentials(address="a@x.com", password="p", imap_host="imap.x.com", folder="Posted loads"),
        smtp=False,
    )
    assert result.ok and result.inbox_messages == 12 and result.alerts_today == 3
    assert calls == [("imap", "imap.x.com", 993)]


# --- storing -----------------------------------------------------------------


def test_a_mailbox_is_stored_encrypted_after_it_signs_in(client, session, account, admin_on, signs_in):
    resp = client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account"]["transport"] == "imap"
    assert body["account"]["status"] == "active"
    assert body["result"]["ok"] is True
    assert signs_in[0].address == "alex@spaceline23llc.com"
    assert signs_in[0].smtp_port == 465

    row = session.query(models.EmailAccount).filter_by(user_id=4).one()
    assert row.uses_imap
    assert row.nylas_grant_id.startswith("imap:")
    assert row.secret != CREDS["password"]
    assert secrets.decrypt(row.secret) == CREDS["password"]
    assert row.connected_by_user_id == ADMIN
    assert "password" not in resp.text.lower() or CREDS["password"] not in resp.text


def test_a_mailbox_that_does_not_sign_in_is_not_stored(client, session, account, admin_on, refuses):
    resp = client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    assert resp.status_code == 422
    assert resp.json()["code"] == "check_failed"
    assert "Invalid credentials" in resp.json()["result"]["imap"]["error"]
    assert session.query(models.EmailAccount).filter_by(user_id=4).count() == 0


def test_connecting_a_nylas_user_replaces_and_revokes_their_grant(
    client, session, account, own_account, admin_on, signs_in, monkeypatch
):
    revoked = []

    async def revoke(grant_id):
        revoked.append(grant_id)

    monkeypatch.setattr(nylas, "revoke_grant", revoke)

    resp = client.put("/api/v1/mailboxes/users/7", headers=_h(), json=dict(CREDS, email_address="jane@gmail.com"))
    assert resp.status_code == 200, resp.text
    assert revoked == ["grant-own-7"]
    session.refresh(own_account)
    assert own_account.uses_imap
    assert own_account.nylas_grant_id.startswith("imap:")
    assert own_account.status == "active"


def test_editing_without_a_password_keeps_the_stored_one(client, session, account, admin_on, signs_in):
    client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    resp = client.put(
        "/api/v1/mailboxes/users/4", headers=_h(),
        json=dict(CREDS, password=None, smtp_port=587),
    )
    assert resp.status_code == 200, resp.text
    assert signs_in[-1].password == CREDS["password"]
    assert signs_in[-1].smtp_port == 587
    row = session.query(models.EmailAccount).filter_by(user_id=4).one()
    assert row.smtp_port == 587


def test_without_a_stored_password_an_edit_needs_one(client, account, admin_on, signs_in):
    resp = client.put("/api/v1/mailboxes/users/4", headers=_h(), json=dict(CREDS, password=None))
    assert resp.status_code == 422


def test_without_the_secret_key_nothing_is_stored(client, account, admin_on, signs_in, monkeypatch):
    monkeypatch.setattr(config, "MAILBOX_SECRET_KEY", "")
    resp = client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    assert resp.status_code == 500
    assert "MAILBOX_SECRET_KEY" in resp.json()["detail"]


def test_the_list_shows_every_mailbox_and_hides_passwords(client, session, account, own_account, admin_on, signs_in):
    client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    resp = client.get("/api/v1/mailboxes", headers=_h())
    assert resp.status_code == 200
    body = resp.json()
    by_user = {a["user_id"]: a for a in body["accounts"]}
    assert by_user[4]["transport"] == "imap"
    assert by_user[7]["transport"] == "nylas"
    assert body["shared"]["account"]["email_address"] == account.email_address
    assert body["secret_key_set"] is True
    assert CREDS["password"] not in resp.text
    assert "secret" not in by_user[4]


def test_a_retest_records_the_outcome_on_the_row(client, session, account, admin_on, signs_in, monkeypatch):
    client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)

    def now_refused(creds, smtp=True):
        return imap_smtp.CheckResult(imap_ok=False, imap_error="password revoked", smtp_ok=True)

    monkeypatch.setattr(imap_smtp, "check", now_refused)
    resp = client.post("/api/v1/mailboxes/users/4/test", headers=_h())
    assert resp.status_code == 200
    assert resp.json()["account"]["status"] == "failing"
    assert "password revoked" in resp.json()["account"]["last_error"]


def test_disconnecting_forgets_the_password_and_revokes(client, session, account, admin_on, signs_in):
    client.put("/api/v1/mailboxes/users/4", headers=_h(), json=CREDS)
    resp = client.delete("/api/v1/mailboxes/users/4", headers=_h())
    assert resp.json() == {"connected": False}
    row = session.query(models.EmailAccount).filter_by(user_id=4).one()
    assert row.secret is None
    assert row.status == "revoked"


# --- sending -----------------------------------------------------------------


@pytest.fixture
def imap_account(session, admin_on):
    acct = models.EmailAccount(
        company_id=1, user_id=4, nylas_grant_id="imap:test", email_address="alex@spaceline23llc.com",
        transport=models.TRANSPORT_IMAP, imap_host="imap.gmail.com", imap_port=993,
        smtp_host="smtp.gmail.com", smtp_port=465, secret=secrets.encrypt("abcd efgh ijkl mnop"),
    )
    session.add(acct)
    session.commit()
    return acct


@pytest.fixture
def smtp_server(monkeypatch):
    """A stand-in SMTP server that records the login and the message."""
    seen = {"logins": [], "messages": [], "ports": []}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            seen["ports"].append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            seen["logins"].append((user, password))

        def send_message(self, msg, from_addr=None, to_addrs=None):
            seen["messages"].append((msg, from_addr, to_addrs))

    monkeypatch.setattr(imap_smtp.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(imap_smtp.smtplib, "SMTP", FakeSMTP)
    return seen


async def test_a_bid_from_an_app_password_mailbox_goes_out_over_smtp_as_a_reply(
    session, account, imap_account, stubs, smtp_server, monkeypatch, load_snapshot  # noqa: F811
):
    load_snapshot.update(POSTING)

    async def never(**kwargs):
        raise AssertionError("Nylas must not be used for this mailbox")

    monkeypatch.setattr(nylas, "send_message", never)
    monkeypatch.setattr(nylas, "find_message_by_rfc_id", never)

    negotiation = await _bid(session, user_id=4)

    assert smtp_server["ports"] == [("smtp.gmail.com", 465)]
    assert smtp_server["logins"] == [("alex@spaceline23llc.com", "abcd efgh ijkl mnop")]
    ((msg, from_addr, to_addrs),) = smtp_server["messages"]
    assert from_addr == "alex@spaceline23llc.com"
    assert to_addrs == [negotiation.broker_email]
    assert msg["In-Reply-To"] == "<" + POSTING["email_message_id"].strip("<>") + ">"
    assert msg["Subject"] == "Re: " + POSTING["email_subject"]
    assert msg["Reply-To"] is None and msg["Cc"] is None
    assert msg.get_body(preferencelist=("html",)) is not None

    outbound_row = session.query(models.EmailMessage).filter_by(negotiation_id=negotiation.id).one()
    assert outbound_row.rfc_message_id == str(msg["Message-ID"]).strip("<>")
    assert negotiation.email_account_id == imap_account.id


async def test_the_smtp_send_uses_starttls_below_465(session, imap_account, smtp_server):
    imap_account.smtp_port = 587
    session.commit()
    await outbound.send(imap_account, to_email="b@x.com", subject="s", body="<p>hi</p>")
    assert smtp_server["ports"] == [("smtp.gmail.com", 587)]


async def test_an_smtp_failure_is_reported_as_a_send_error(session, imap_account, monkeypatch):
    def boom(creds, msg):
        raise imap_smtp.smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(imap_smtp, "send", boom)
    with pytest.raises(outbound.SendError) as err:
        await outbound.send(imap_account, to_email="b@x.com", subject="s", body="b")
    assert "not accepted" in str(err.value)


def test_a_redirected_test_server_drops_the_cc_and_rewrites_the_recipient():
    msg = imap_smtp.build_message(
        sender="a@x.com", to_email="b@y.com", subject="s", html="<p>x</p>", text="x",
        in_reply_to="<alert@sylectus>", reply_to=["a@x.com", "shared@x.com"], cc=["shared@x.com"],
    )
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert parsed["References"] == "<alert@sylectus>"
    assert parsed["Reply-To"] == "a@x.com, shared@x.com"
    assert parsed["Cc"] == "shared@x.com"
    assert parsed["Message-ID"].endswith("@x.com>")


def test_an_app_password_mailbox_needs_no_detour_to_the_shared_one(session, account, imap_account):
    assert catcher_headers(session, imap_account) == {}


async def test_the_posting_lookup_is_skipped_for_an_app_password_mailbox(imap_account, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("no Nylas lookup for an IMAP mailbox")

    monkeypatch.setattr(nylas, "find_message_by_rfc_id", never)
    nylas_id, subject = await find_posting_email(imap_account, dict(POSTING))
    assert nylas_id is None
    assert subject == POSTING["email_subject"]


# --- reading -----------------------------------------------------------------


def test_the_reader_lists_every_app_password_mailbox_after_the_shared_one(session, account, imap_account, monkeypatch):
    monkeypatch.setattr(config, "SHARED_INBOX_ADDRESS", "dispatch@shipluxellc.com")
    monkeypatch.setattr(config, "SHARED_INBOX_APP_PASSWORD", "shared-pw")
    found = shared_inbox.sources(session)
    assert [(s.address, s.across_accounts, s.password) for s in found] == [
        ("dispatch@shipluxellc.com", True, "shared-pw"),
        ("alex@spaceline23llc.com", False, "abcd efgh ijkl mnop"),
    ]
    assert found[1].account_id == imap_account.id


def test_a_revoked_or_nylas_mailbox_is_not_read(session, account, own_account, imap_account, monkeypatch):
    monkeypatch.setattr(config, "SHARED_INBOX_ADDRESS", "")
    imap_account.status = "revoked"
    session.commit()
    assert shared_inbox.sources(session) == []


async def test_an_imap_pass_that_cannot_sign_in_marks_the_mailbox_failing(session, imap_account, monkeypatch):
    def refused(last_uid, source):
        raise imap_smtp.imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    monkeypatch.setattr(shared_inbox, "_read_batch", refused)
    source = shared_inbox.sources(session)[0]
    with pytest.raises(imap_smtp.imaplib.IMAP4.error):
        await shared_inbox.poll_source(source, {})
    session.expire_all()
    assert imap_account.status == "failing"
    assert "AUTHENTICATIONFAILED" in imap_account.last_error

    monkeypatch.setattr(shared_inbox, "_read_batch", lambda last_uid, source: (1, 10, []))
    await shared_inbox.poll_source(source, {})
    session.expire_all()
    assert imap_account.status == "active"
    assert imap_account.last_error is None


async def test_a_dispatchers_inbox_answers_only_for_its_own_bids(session, account, imap_account, monkeypatch):
    handled = []

    async def record(session, account, message, across_accounts=False):
        handled.append((account.email_address, across_accounts))

    monkeypatch.setattr("services.inbound.handle_inbound_message", record)
    from tests.test_shared_inbox import raw_email

    source = shared_inbox.sources(session)[0]
    await shared_inbox.handle(3, raw_email(to="alex@spaceline23llc.com", cc=""), None, 1, source)
    assert handled == [("alex@spaceline23llc.com", False)]
    cursor = session.get(models.SharedInboxCursor, "alex@spaceline23llc.com")
    assert cursor.last_uid == 3
