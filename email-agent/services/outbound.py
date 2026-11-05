"""
One door out for every email this service sends.

A mailbox connected through Nylas sends through its grant, exactly as
before. A mailbox connected with an app password (EmailAccount.transport
"imap") sends over SMTP from its own address with In-Reply-To set, which
threads the bid under the Sylectus alert in the dispatcher's own Gmail and
on the broker's side alike. Both return the dict the callers already read:
`id`, `thread_id` and `headers` with the Message-ID.
"""
import asyncio
import logging
from typing import Optional

from database import models
from services import bid_email, imap_smtp, secrets
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)


class SendError(NylasError):
    """An SMTP send that failed. Subclasses NylasError so every caller's
    existing handling (a 502 with the reason) applies unchanged."""


def credentials(account: models.EmailAccount) -> imap_smtp.Credentials:
    if not account.secret:
        raise SendError(f"{account.email_address} has no app password stored; connect it again")
    return imap_smtp.Credentials(
        address=account.email_address,
        password=secrets.decrypt(account.secret),
        imap_host=account.imap_host or "",
        imap_port=int(account.imap_port or 993),
        smtp_host=account.smtp_host or "",
        smtp_port=int(account.smtp_port or 465),
    )


async def send(
    account: models.EmailAccount,
    *,
    to_email: str,
    subject: str,
    body: str,
    reply_to_message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    reply_to: Optional[list[str]] = None,
    cc: Optional[list[str]] = None,
) -> dict:
    if not account.uses_imap:
        extra = {}
        if reply_to:
            extra["reply_to"] = reply_to
        if cc:
            extra["cc"] = cc
        return await nylas.send_message(
            grant_id=account.nylas_grant_id,
            to_email=to_email,
            subject=subject,
            body=body,
            reply_to_message_id=reply_to_message_id,
            in_reply_to=in_reply_to,
            **extra,
        )

    from config.settings import config  # local: tests monkeypatch the instance

    if config.EMAIL_REDIRECT_TO:
        logger.warning(
            f"EMAIL_REDIRECT_TO is set: '{subject}' meant for {to_email} "
            f"goes to {config.EMAIL_REDIRECT_TO} instead"
        )
        body = f"<p><b>[TEST — intended recipient: {to_email}]</b></p>{body}"
        to_email = config.EMAIL_REDIRECT_TO
        cc = None

    try:
        creds = credentials(account)
    except secrets.SecretError as e:
        raise SendError(str(e))
    msg = imap_smtp.build_message(
        sender=account.email_address,
        to_email=to_email,
        subject=subject,
        html=body,
        text=bid_email.html_to_text(body),
        in_reply_to=in_reply_to,
        reply_to=reply_to,
        cc=cc,
    )
    try:
        rfc_id = await asyncio.to_thread(imap_smtp.send, creds, msg)
    except Exception as e:  # noqa: BLE001 - smtplib raises a family of its own
        logger.error(f"SMTP send from {account.email_address} failed: {e}")
        raise SendError(f"{account.email_address} could not send over SMTP: {imap_smtp._plain(e)}")
    logger.info(
        f"sent '{subject}' from {account.email_address} over SMTP"
        + (f" as a reply: In-Reply-To <{in_reply_to.strip().strip('<>')}>" if in_reply_to else "")
    )
    return {"id": None, "thread_id": None, "headers": [{"name": "Message-Id", "value": f"<{rfc_id}>"}]}
