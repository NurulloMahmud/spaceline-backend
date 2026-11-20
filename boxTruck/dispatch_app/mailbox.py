"""The BTM user's bid mailbox address, read from the email agent.

Mailboxes (Admin > Mailboxes, IMAP/SMTP per dispatcher) live in the email
agent's database, so Django asks its internal route. Any failure is "no
mailbox" (null): the app then keeps Send disabled, which is the safe side.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 3


def mailbox_for(user_id: int) -> str | None:
    base = (getattr(settings, "EMAIL_AGENT_INTERNAL_URL", "") or "").rstrip("/")
    if not base:
        return None
    try:
        resp = requests.get(
            f"{base}/internal/v1/accounts/users/{int(user_id)}",
            headers={"X-Internal-Secret": settings.INTERNAL_SERVICE_SECRET},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("mailbox of user %s not read from the email agent: %s", user_id, exc)
        return None
    if not isinstance(body, dict) or not body.get("connected"):
        return None
    address = str(body.get("email_address") or "").strip()
    return address or None
