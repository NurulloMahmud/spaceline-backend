"""
Plain IMAP and SMTP for a mailbox connected with an app password.

Everything here is synchronous (imaplib, smtplib) and run by the callers in
a thread. Nothing touches a provider API, so a mailbox that receives the
whole Sylectus feed costs nothing to send from: IMAP and SMTP have no
per-minute unit quota.
"""
import email.utils
import imaplib
import logging
import smtplib
import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as MimeMessage
from typing import Optional

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
ALERT_SENDER_DOMAIN = "sylectus.com"


@dataclass
class Credentials:
    address: str
    password: str
    imap_host: str
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 465
    folder: str = "INBOX"


@dataclass
class CheckResult:
    imap_ok: bool = False
    imap_error: str = ""
    inbox_messages: Optional[int] = None
    smtp_ok: bool = False
    smtp_error: str = ""
    alerts_today: Optional[int] = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.imap_ok and self.smtp_ok

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "imap": {"ok": self.imap_ok, "error": self.imap_error or None, "inbox_messages": self.inbox_messages},
            "smtp": {"ok": self.smtp_ok, "error": self.smtp_error or None},
            "alerts_today": self.alerts_today,
            "checked_at": self.checked_at.isoformat(),
        }


_SIGN_IN_WORDS = (
    "authenticationfailed",
    "invalid credentials",
    "username and password not accepted",
    "application-specific password",
    "login failed",
    "[auth]",
)


def sign_in_refused(e: Exception) -> bool:
    """The provider turned the password down, over IMAP or SMTP. The one
    failure that is the mailbox's own and stays until someone fixes it."""
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return True
    if isinstance(e, imaplib.IMAP4.error):
        text = str(e).lower()
        return any(w in text for w in _SIGN_IN_WORDS)
    return False


def transient(e: Exception) -> bool:
    """A failure that passes on its own: a busy or restarting server, a slow
    or dropped connection. Worth another try; a refused password, sender or
    recipient is not."""
    if sign_in_refused(e):
        return False
    if isinstance(e, smtplib.SMTPResponseException):
        return 400 <= int(e.smtp_code) < 500
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return all(400 <= int(code) < 500 for code, _ in e.recipients.values())
    if isinstance(e, (smtplib.SMTPServerDisconnected, imaplib.IMAP4.abort)):
        return True
    return isinstance(e, (TimeoutError, ConnectionError, ssl.SSLError, OSError))


def _plain(e: Exception) -> str:
    """The provider's words, without the exception class around them."""
    text = str(e)
    if isinstance(e, (socket.gaierror, ConnectionRefusedError, socket.timeout, TimeoutError, OSError)) and not text:
        text = type(e).__name__
    if isinstance(e, bytes):
        text = e.decode(errors="replace")
    return text.strip().strip("[]").strip()[:300] or type(e).__name__


def check(creds: Credentials, smtp: bool = True) -> CheckResult:
    """Sign in both ways (or IMAP alone, for a mailbox that is only read) and
    count what is in the folder. Never raises."""
    result = CheckResult()
    try:
        conn = imaplib.IMAP4_SSL(creds.imap_host, creds.imap_port, timeout=TIMEOUT_SECONDS)
        try:
            conn.login(creds.address, creds.password)
            typ, data = conn.select(_quote(creds.folder), readonly=True)
            if typ != "OK":
                raise imaplib.IMAP4.error(f"folder {creds.folder!r} cannot be opened")
            result.inbox_messages = int(data[0]) if data and data[0] else 0
            since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = conn.uid("search", None, f'(SINCE {since} FROM "{ALERT_SENDER_DOMAIN}")')
            if typ == "OK":
                result.alerts_today = len(data[0].split()) if data and data[0] else 0
            result.imap_ok = True
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001 - every failure is reported, not raised
        result.imap_error = _plain(e)

    if not smtp:
        result.smtp_ok = True
        return result
    try:
        with _smtp(creds) as conn:
            conn.login(creds.address, creds.password)
        result.smtp_ok = True
    except Exception as e:  # noqa: BLE001
        result.smtp_error = _plain(e)
    return result


def _quote(folder: str) -> str:
    return folder if folder.upper() == "INBOX" else f'"{folder}"'


def _smtp(creds: Credentials) -> smtplib.SMTP:
    """465 is implicit TLS; anything else starts plain and upgrades."""
    context = ssl.create_default_context()
    if int(creds.smtp_port) == 465:
        return smtplib.SMTP_SSL(creds.smtp_host, creds.smtp_port, timeout=TIMEOUT_SECONDS, context=context)
    smtp = smtplib.SMTP(creds.smtp_host, creds.smtp_port, timeout=TIMEOUT_SECONDS)
    smtp.ehlo()
    smtp.starttls(context=context)
    smtp.ehlo()
    return smtp


def build_message(
    *,
    sender: str,
    to_email: str,
    subject: str,
    html: str,
    text: str,
    in_reply_to: Optional[str] = None,
    reply_to: Optional[list[str]] = None,
    cc: Optional[list[str]] = None,
) -> MimeMessage:
    """
    The email as the broker's client will see it: text and HTML alternatives,
    In-Reply-To and References so it threads under the alert, and a
    Message-ID of our own so the answer can be matched to it later.
    """
    msg = MimeMessage()
    msg["From"] = sender
    msg["To"] = to_email
    if cc:
        msg["Cc"] = ", ".join(c for c in cc if c)
    if reply_to:
        msg["Reply-To"] = ", ".join(r for r in reply_to if r)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg["Message-ID"] = email.utils.make_msgid(domain=sender.rsplit("@", 1)[-1] or None)
    bare = (in_reply_to or "").strip().strip("<>").strip()
    if bare:
        msg["In-Reply-To"] = f"<{bare}>"
        msg["References"] = f"<{bare}>"
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send(creds: Credentials, msg: MimeMessage) -> str:
    """Submit the message; returns its Message-ID without angle brackets."""
    recipients = []
    for header in ("To", "Cc"):
        for _, addr in email.utils.getaddresses([str(v) for v in msg.get_all(header, [])]):
            if addr:
                recipients.append(addr)
    with _smtp(creds) as smtp:
        smtp.login(creds.address, creds.password)
        smtp.send_message(msg, from_addr=creds.address, to_addrs=recipients)
    return str(msg["Message-ID"]).strip().strip("<>")
