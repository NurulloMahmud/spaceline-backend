"""
Which mailbox a broker's answer has to reach.

A bid sent from a dispatcher's own mailbox would be answered to that mailbox,
and nothing reads it any more: Gmail charges every read Nylas makes against
the mailbox's own API quota, and a mailbox on the Sylectus alert list spends
that quota on alerts, so Nylas no longer watches any mailbox. Instead every
email a dispatcher's mailbox sends names the company's shared mailbox as
Reply-To (next to the dispatcher, who keeps seeing the conversation in their
own Gmail) and as Cc, so a Reply All reaches it too. The shared mailbox is
read over IMAP by services.shared_inbox.
"""
from typing import Optional

from sqlalchemy.orm import Session

from config.settings import config
from database import models


def reply_catcher(session: Session, account: models.EmailAccount) -> Optional[str]:
    """
    The address a broker's answer to `account`'s email must also reach.
    None when that is the account itself: the shared mailbox is the one
    being read, so its own mail needs no detour.
    """
    own = (account.email_address or "").strip().lower()
    address = config.SHARED_INBOX_ADDRESS
    if not address:
        shared = (
            session.query(models.EmailAccount)
            .filter(
                models.EmailAccount.company_id == account.company_id,
                models.EmailAccount.user_id.is_(None),
            )
            .first()
        )
        address = (shared.email_address or "").strip().lower() if shared else ""
    if not address or address == own:
        return None
    return address


def catcher_headers(session: Session, account: models.EmailAccount) -> dict:
    """
    Keyword arguments for nylas.send_message that bring the broker's answer
    to the shared mailbox. Empty when the sender is the shared mailbox.
    """
    catcher = reply_catcher(session, account)
    if not catcher:
        return {}
    return {"reply_to": [account.email_address, catcher], "cc": [catcher]}
