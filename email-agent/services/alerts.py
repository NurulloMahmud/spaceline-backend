"""
Operator alerts over Telegram.

A dispatcher who cannot bid finds out at the worst moment; whoever runs the
service should hear first. Four conditions are watched, and each is said
once when it starts, repeated every hour while it lasts, and said once more
when it clears:

- a mailbox whose password the provider refused (its bids stop until an
  administrator enters a working app password),
- a bid that could not be sent after the retries in services.outbound,
- a mailbox whose inbox reads have failed on every pass for a while,
- the inbox reader itself not finishing a pass for a while.

The bot and chat come from ALERT_BOT_TOKEN / ALERT_CHAT_ID, which default to
the IP-alert bot. With neither set the alerts only reach the log.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from config.settings import config
from database import models
from database.connection import get_session
from services import telegram

logger = logging.getLogger(__name__)

CHECK_SECONDS = 60
# No pass finished for this long: the reader is stuck or the process is wedged.
READER_QUIET_MINUTES = 5
# Every pass on a mailbox failing for this long: the inbox is not being read.
READS_FAILING_MINUTES = 10
# A standing alert is repeated this often.
REPEAT_MINUTES = 60
# One send-failure alert per mailbox in this window.
SEND_FAILURE_MINUTES = 10

_standing: dict[str, datetime] = {}      # condition -> last time it was sent
_failing_since: dict[str, datetime] = {}  # mailbox -> first pass seen failing
# The reader records when it last finished a pass; the watchdog reads it.
last_reader_pass: Optional[datetime] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return bool(config.ALERT_BOT_TOKEN and config.ALERT_CHAT_ID)


async def notify(text: str) -> bool:
    """One plain-text message to the alert chat; the log when there is none."""
    if not enabled():
        logger.warning(f"alert (no ALERT_CHAT_ID / ALERT_BOT_TOKEN set): {text}")
        return False
    ok = await telegram.send_message(config.ALERT_CHAT_ID, text, bot_token=config.ALERT_BOT_TOKEN, parse_mode=None)
    if not ok:
        logger.error(f"alert could not be delivered: {text}")
    return ok


async def _raise(key: str, text: str, now: datetime, repeat: timedelta) -> Optional[str]:
    last = _standing.get(key)
    if last is not None and now - last < repeat:
        return None
    _standing[key] = now
    await notify(("ALERT: " if last is None else "STILL: ") + text)
    return text


async def _clear(key: str, text: str) -> Optional[str]:
    if _standing.pop(key, None) is None:
        return None
    await notify("CLEARED: " + text)
    return text


def send_failed(address: str, error: str, tries: int) -> None:
    """Called by services.outbound after the last try. Fire and forget: the
    dispatcher's error must not wait on Telegram."""
    now = _now()
    key = f"send:{address}"
    last = _standing.get(key)
    if last is not None and now - last < timedelta(minutes=SEND_FAILURE_MINUTES):
        return
    _standing[key] = now
    text = f"a bid from {address} could not be sent after {tries} tr{'y' if tries == 1 else 'ies'}: {error}"
    try:
        asyncio.get_running_loop().create_task(notify("ALERT: " + text))
    except RuntimeError:
        logger.warning(f"alert (no event loop): {text}")


async def check(session, now: Optional[datetime] = None) -> list[str]:
    """One look at every mailbox and at the reader. Returns what it said."""
    now = now or _now()
    repeat = timedelta(minutes=REPEAT_MINUTES)
    said: list[str] = []
    rows = (
        session.query(models.EmailAccount)
        .filter(
            models.EmailAccount.transport == models.TRANSPORT_IMAP,
            models.EmailAccount.status.in_(("active", "failing")),
            models.EmailAccount.secret.isnot(None),
        )
        .all()
    )
    seen = set()
    for row in rows:
        address = (row.email_address or "").strip().lower()
        seen.add(address)
        who = f"mailbox {address}" + (f" (user {row.user_id})" if row.user_id else " (shared)")

        if row.status == "failing":
            text = await _raise(
                f"password:{address}",
                f"{who}: password refused ({row.last_error or 'no detail'}). Its bids stop until an "
                "administrator enters a working app password under Admin > Mailboxes.",
                now, repeat,
            )
        else:
            text = await _clear(f"password:{address}", f"{who} signs in again.")
        if text:
            said.append(text)

        if row.status == "active" and row.last_error:
            since = _failing_since.setdefault(address, now)
            if now - since >= timedelta(minutes=READS_FAILING_MINUTES):
                text = await _raise(
                    f"reads:{address}",
                    f"{who}: inbox reads have failed for {int((now - since).total_seconds() // 60)} minutes "
                    f"({row.last_error}). Broker replies to its bids are not being picked up.",
                    now, repeat,
                )
                if text:
                    said.append(text)
        else:
            _failing_since.pop(address, None)
            text = await _clear(f"reads:{address}", f"{who}: inbox reads are back.")
            if text:
                said.append(text)

    for address in [a for a in _failing_since if a not in seen]:
        _failing_since.pop(address, None)
    for key in [k for k in _standing if k.split(":", 1)[0] in ("password", "reads") and k.split(":", 1)[1] not in seen]:
        _standing.pop(key, None)

    if last_reader_pass is not None and now - last_reader_pass >= timedelta(minutes=READER_QUIET_MINUTES):
        text = await _raise(
            "reader",
            f"the inbox reader has not finished a pass for {int((now - last_reader_pass).total_seconds() // 60)} "
            "minutes. No broker reply is being picked up; the email-agent may need a restart.",
            now, repeat,
        )
    else:
        text = await _clear("reader", "the inbox reader is finishing passes again.")
    if text:
        said.append(text)
    return said


async def watch_forever() -> None:
    """Runs for the life of the process. A failed check never stops the loop."""
    logger.info(
        f"mailbox watchdog checking every {CHECK_SECONDS}s"
        + (" and alerting over Telegram" if enabled() else "; no alert chat set, so it only logs")
    )
    while True:
        try:
            await asyncio.sleep(CHECK_SECONDS)
            with get_session() as session:
                await check(session)
        except asyncio.CancelledError:
            logger.info("mailbox watchdog stopping")
            raise
        except Exception:
            logger.exception("mailbox watchdog check failed; continuing")
