"""
The shared mailbox, read over IMAP.

Every email a dispatcher's own mailbox sends names the company's shared
mailbox as Reply-To and Cc (services.mailboxes), so the broker's answer lands
here as well as in the dispatcher's inbox. This reader polls the folder with
an app password, turns each new message into the dict the inbound pipeline
already understands, and hands it to inbound.handle_inbound_message with
every negotiation of the company as a candidate.

Nothing here touches the Gmail API, so nothing here spends the quota that
was refusing bids: IMAP has no per-minute unit budget.

The reader keeps its place as (UIDVALIDITY, last UID) in the database, so a
restart carries on where it left off, and a mailbox seen for the first time
starts at its end rather than replaying history. A message the pipeline
fails on is tried on a few passes, then skipped with an error, so one bad
email cannot stop every one behind it.
"""
import asyncio
import email
import imaplib
import logging
import re
from email import policy
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError

from config.settings import config
from database import models
from database.connection import get_session
from services import inbound

logger = logging.getLogger(__name__)

ALERT_SENDER_DOMAIN = "sylectus.com"

# Messages taken from the folder in one pass; the rest wait for the next.
BATCH_LIMIT = 50
# Passes a message that keeps failing is given before it is skipped.
MAX_ATTEMPTS = 3

_THRID = re.compile(rb"X-GM-THRID (\d+)")
_UID = re.compile(rb"UID (\d+)")


class Mailbox:
    """One IMAP session on the shared folder, read-only. Synchronous: the
    caller runs it in a thread."""

    def __init__(self, host: str, user: str, password: str, folder: str):
        self.host, self.user, self.password, self.folder = host, user, password, folder
        self.conn: Optional[imaplib.IMAP4_SSL] = None
        self.uidvalidity = 0
        self.gmail = "gmail" in host.lower()

    def __enter__(self) -> "Mailbox":
        self.conn = imaplib.IMAP4_SSL(self.host)
        self.conn.login(self.user, self.password)
        typ, _ = self.conn.select(_quote(self.folder), readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot select folder {self.folder!r}")
        typ, data = self.conn.response("UIDVALIDITY")
        self.uidvalidity = int(data[0]) if data and data[0] else 0
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.conn is not None:
                self.conn.logout()
        except Exception:
            pass

    def last_uid(self) -> int:
        typ, data = self.conn.uid("search", None, "ALL")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        return int(uids[-1]) if uids else 0

    def uids_after(self, last: int) -> list[int]:
        """UIDs above `last`, oldest first. The server answers a range past
        the end with its last message, hence the filter."""
        typ, data = self.conn.uid("search", None, f"UID {last + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        return sorted(int(u) for u in data[0].split() if int(u) > last)

    def fetch(self, uid: int) -> tuple[bytes, Optional[str]]:
        """The raw message and, on Gmail, its thread id as Nylas spells it
        (hexadecimal), the same value a message read through Nylas carries."""
        items = "(UID X-GM-THRID BODY.PEEK[])" if self.gmail else "(UID BODY.PEEK[])"
        typ, data = self.conn.uid("fetch", str(uid), items)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"fetch of UID {uid} failed: {typ}")
        raw = b""
        thread_id = None
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                meta, body = part
                raw = body
                found = _THRID.search(meta or b"")
                if found:
                    thread_id = format(int(found.group(1)), "x")
        if not raw:
            raise imaplib.IMAP4.error(f"fetch of UID {uid} returned no body")
        return raw, thread_id


def _quote(folder: str) -> str:
    return folder if folder.upper() == "INBOX" else f'"{folder}"'


def _people(msg: MimeMessage, header: str) -> list[dict]:
    values = msg.get_all(header, [])
    out = []
    for name, address in getaddresses([str(v) for v in values]):
        address = (address or "").strip().lower()
        if address:
            out.append({"name": (name or "").strip(), "email": address})
    return out


def parse_message(raw: bytes, message_id: Optional[str], thread_id: Optional[str]) -> dict:
    """
    The dict inbound.handle_inbound_message reads, in the shape Nylas gives:
    people as {name, email}, the HTML body when there is one, PDF attachments
    with their bytes under `content` so no download is needed, and the
    threading headers under `headers`.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)

    body = ""
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        body = html_part.get_content()
    else:
        text_part = msg.get_body(preferencelist=("plain",))
        if text_part is not None:
            body = text_part.get_content()

    attachments = []
    for index, part in enumerate(msg.iter_attachments()):
        filename = part.get_filename() or ""
        content_type = part.get_content_type() or ""
        if not (filename.lower().endswith(".pdf") or "pdf" in content_type.lower()):
            continue
        content = part.get_payload(decode=True) or b""
        attachments.append({
            "id": f"{message_id}:{index}" if message_id else str(index),
            "filename": filename or "attachment.pdf",
            "size": len(content),
            "content_type": content_type,
            "content": content,
        })

    date = None
    try:
        if msg.get("Date"):
            date = int(parsedate_to_datetime(str(msg["Date"])).timestamp())
    except (TypeError, ValueError):
        date = None

    return {
        "id": message_id,
        "thread_id": thread_id,
        "from": _people(msg, "From"),
        "reply_to": _people(msg, "Reply-To"),
        "to": _people(msg, "To"),
        "cc": _people(msg, "Cc"),
        "subject": str(msg.get("Subject") or ""),
        "body": body,
        "snippet": " ".join(inbound.strip_quoted(body).split())[:200],
        "date": date,
        "attachments": attachments,
        "headers": {
            name: str(msg.get(name) or "")
            for name in ("Message-Id", "In-Reply-To", "References")
            if msg.get(name)
        },
    }


def catcher_account(session) -> Optional[models.EmailAccount]:
    """The shared EmailAccount the folder belongs to. Messages are stored
    against it, and its company bounds the negotiations they can match."""
    return (
        session.query(models.EmailAccount)
        .filter(
            models.EmailAccount.user_id.is_(None),
            models.EmailAccount.email_address.ilike(config.SHARED_INBOX_ADDRESS),
        )
        .first()
    )


def own_addresses(session, company_id: int) -> set[str]:
    """Every mailbox of the company, including the shared one: a message from
    any of them is our own copy of a send, not a broker's answer."""
    rows = (
        session.query(models.EmailAccount.email_address)
        .filter(models.EmailAccount.company_id == company_id)
        .all()
    )
    out = {(r[0] or "").strip().lower() for r in rows}
    out.add(config.SHARED_INBOX_ADDRESS)
    return {a for a in out if a}


def from_alert_domain(address: str) -> bool:
    """Sent by Sylectus, from its domain or any host under it (alerts come
    from out01.sylectus.com and the like)."""
    domain = (address or "").strip().lower().rsplit("@", 1)[-1]
    return domain == ALERT_SENDER_DOMAIN or domain.endswith("." + ALERT_SENDER_DOMAIN)


def is_alert(message: dict) -> bool:
    for entry in (message.get("from") or []) + (message.get("reply_to") or []):
        if from_alert_domain(entry.get("email") or ""):
            return True
    return False


def _read_batch(last_uid: Optional[int]) -> tuple[int, int, list[tuple[int, bytes, Optional[str]]]]:
    """
    One IMAP session: (uidvalidity, folder's last uid, [(uid, raw, thread_id)]).
    With no cursor yet (`last_uid` None) nothing is fetched; the caller records
    the folder's end and starts from there.
    """
    with Mailbox(
        config.SHARED_INBOX_IMAP_HOST,
        config.SHARED_INBOX_ADDRESS,
        config.SHARED_INBOX_APP_PASSWORD,
        config.SHARED_INBOX_FOLDER,
    ) as box:
        end = box.last_uid()
        if last_uid is None:
            return box.uidvalidity, end, []
        fetched = []
        for uid in box.uids_after(last_uid)[:BATCH_LIMIT]:
            raw, thread_id = box.fetch(uid)
            fetched.append((uid, raw, thread_id))
        return box.uidvalidity, end, fetched


def _cursor(session, uidvalidity: int) -> Optional[models.SharedInboxCursor]:
    row = session.get(models.SharedInboxCursor, config.SHARED_INBOX_ADDRESS)
    if row and row.uidvalidity == uidvalidity:
        return row
    return None


def _advance(session, uidvalidity: int, uid: int) -> None:
    row = session.get(models.SharedInboxCursor, config.SHARED_INBOX_ADDRESS)
    if row is None:
        session.add(models.SharedInboxCursor(
            mailbox=config.SHARED_INBOX_ADDRESS, uidvalidity=uidvalidity, last_uid=uid,
        ))
    else:
        row.uidvalidity = uidvalidity
        row.last_uid = uid


async def handle(uid: int, raw: bytes, thread_id: Optional[str], uidvalidity: int) -> None:
    """One message, one transaction: the claim, the pipeline and the cursor
    move together, so a failure leaves all three for the next pass."""
    key = f"imap:{config.SHARED_INBOX_ADDRESS}:{uidvalidity}:{uid}"
    with get_session() as session:
        account = catcher_account(session)
        if account is None:
            raise RuntimeError(
                f"no shared mailbox is connected for {config.SHARED_INBOX_ADDRESS}; "
                "connect it from the company settings so its messages have a company"
            )
        session.add(models.ProcessedWebhook(nylas_message_id=key))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            logger.info(f"shared inbox UID {uid} was already handled")
            _advance(session, uidvalidity, uid)
            return

        # The Nylas id is left empty on purpose: a reply to this message
        # threads on its RFC Message-ID, which every mailbox understands.
        message = parse_message(raw, None, thread_id)
        senders = {e["email"] for e in message["from"]}
        if is_alert(message):
            logger.debug(f"shared inbox UID {uid}: Sylectus alert, skipped")
        elif senders & own_addresses(session, account.company_id):
            logger.info(
                f"shared inbox UID {uid}: our own copy of a send from "
                f"{sorted(senders)}, skipped"
            )
        else:
            await inbound.handle_inbound_message(
                session, account, message, across_accounts=True
            )
        _advance(session, uidvalidity, uid)


async def poll_once(attempts: dict[int, int]) -> int:
    """One pass over the folder. Returns how many messages were handled."""
    with get_session() as session:
        row = session.get(models.SharedInboxCursor, config.SHARED_INBOX_ADDRESS)
        known = (row.uidvalidity, row.last_uid) if row else None

    uidvalidity, end, batch = await asyncio.to_thread(
        _read_batch, known[1] if known else None
    )
    if known is None or known[0] != uidvalidity:
        # First sight of this folder, or its UIDs were renumbered: start at
        # the end. Nothing older is a reply to a bid this reader was meant to
        # catch, and a replay of the whole folder would be thousands of
        # messages through the AI.
        with get_session() as session:
            _advance(session, uidvalidity, end)
        logger.info(
            f"shared inbox {config.SHARED_INBOX_ADDRESS}: starting after UID {end} "
            f"(UIDVALIDITY {uidvalidity})"
        )
        attempts.clear()
        return 0
    if not batch:
        return 0

    handled = 0
    for uid, raw, thread_id in batch:
        try:
            await handle(uid, raw, thread_id, uidvalidity)
            attempts.pop(uid, None)
            handled += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            attempts[uid] = attempts.get(uid, 0) + 1
            if attempts[uid] >= MAX_ATTEMPTS:
                logger.exception(
                    f"shared inbox UID {uid} failed {attempts[uid]} times; skipping it. "
                    "The message is still in the mailbox."
                )
                with get_session() as session:
                    _advance(session, uidvalidity, uid)
                attempts.pop(uid, None)
                continue
            logger.exception(
                f"shared inbox UID {uid} failed (attempt {attempts[uid]}); "
                "trying again on the next pass"
            )
            break
    return handled


async def poll_forever() -> None:
    """Runs for the life of the process when the reader is configured. A
    failed pass never stops the loop."""
    if not config.shared_inbox_enabled():
        logger.info("shared inbox reader off: SHARED_INBOX_ADDRESS or its app password is empty")
        return
    logger.info(
        f"shared inbox reader polling {config.SHARED_INBOX_ADDRESS} "
        f"({config.SHARED_INBOX_FOLDER}) every {config.SHARED_INBOX_POLL_SECONDS}s"
    )
    attempts: dict[int, int] = {}
    while True:
        try:
            await asyncio.sleep(config.SHARED_INBOX_POLL_SECONDS)
            await poll_once(attempts)
        except asyncio.CancelledError:
            logger.info("shared inbox reader stopping")
            raise
        except Exception:
            logger.exception("shared inbox pass failed; continuing")
