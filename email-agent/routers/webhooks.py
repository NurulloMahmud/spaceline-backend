"""
Nylas webhook receiver — called by Nylas directly, not by a peer service.

The `/internal` prefix is about audience, not reachability: this path is
public and unauthenticated by JWT. Its only credential is the HMAC signature
Nylas computes with the webhook secret, so `NYLAS_WEBHOOK_SECRET` must hold
the secret Nylas returned when the webhook was registered, or every delivery
is rejected.

Answers fast and processes in the background: Nylas retries on a slow or
failed response, and the AI + parse pipeline takes far longer than its timeout.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from database import models
from database.connection import get_session
from services import inbound
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1/webhooks", tags=["webhooks"])

MESSAGE_TRIGGERS = ("message.created",)

# A grant dies when the mailbox owner revokes access or their provider
# password changes. Nothing arrives after that and every send fails, so the
# account is flagged rather than left looking healthy on the settings page.
GRANT_TRIGGERS = {
    "grant.expired": "expired",
    "grant.deleted": "revoked",
}


@router.get("/nylas")
async def nylas_challenge(challenge: str = ""):
    """Nylas verifies the endpoint by echoing this value back."""
    return PlainTextResponse(challenge)


@router.post("/nylas")
async def nylas_webhook(
    request: Request,
    background: BackgroundTasks,
    x_nylas_signature: str = Header(default=""),
):
    raw = await request.body()
    if not nylas.verify_webhook(x_nylas_signature, raw):
        logger.warning("rejected a webhook with an invalid signature")
        raise HTTPException(401, "invalid signature")

    payload = await request.json()
    trigger = payload.get("type", "")
    data = payload.get("data") or {}
    obj = data.get("object") or {}
    grant_id = obj.get("grant_id") or data.get("grant_id")

    if trigger in GRANT_TRIGGERS:
        if not grant_id:
            return JSONResponse({"ok": True, "ignored": "incomplete"})
        background.add_task(mark_grant_dead, grant_id, GRANT_TRIGGERS[trigger])
        return JSONResponse({"ok": True})

    if trigger not in MESSAGE_TRIGGERS:
        return JSONResponse({"ok": True, "ignored": trigger})

    message_id = obj.get("id")
    if not grant_id or not message_id:
        logger.warning(f"webhook {trigger} missing grant or message id")
        return JSONResponse({"ok": True, "ignored": "incomplete"})

    background.add_task(process_message, grant_id, message_id)
    return JSONResponse({"ok": True})


def mark_grant_dead(grant_id: str, status: str) -> None:
    """The mailbox stopped working. Surfaced through GET /api/v1/accounts."""
    try:
        with get_session() as session:
            account = (
                session.query(models.EmailAccount)
                .filter(models.EmailAccount.nylas_grant_id == grant_id)
                .first()
            )
            if not account:
                logger.warning(f"grant webhook for unknown grant {grant_id}")
                return
            account.status = status
            logger.error(
                "mailbox %s (company %s) is %s — it must be reconnected",
                account.email_address, account.company_id, status,
            )
    except Exception:
        logger.exception(f"could not flag grant {grant_id} as {status}")


async def process_message(grant_id: str, message_id: str) -> None:
    """
    Runs after the webhook response.

    The whole body is one transaction. Anything that goes wrong rolls it back,
    including the processed-webhook claim, so the message is genuinely retried
    when Nylas redelivers it instead of being marked done and dropped.
    """
    try:
        with get_session() as session:
            account = (
                session.query(models.EmailAccount)
                .filter(models.EmailAccount.nylas_grant_id == grant_id)
                .first()
            )
            if not account:
                logger.warning(f"webhook for unknown grant {grant_id}")
                return

            seen = (
                session.query(models.ProcessedWebhook)
                .filter(models.ProcessedWebhook.nylas_message_id == message_id)
                .first()
            )
            if seen:
                logger.info(f"webhook message {message_id} already processed")
                return

            # Claimed up front so a concurrent redelivery collides on the
            # unique index rather than running the pipeline twice.
            session.add(models.ProcessedWebhook(nylas_message_id=message_id))
            session.flush()

            message = await nylas.get_message(grant_id, message_id)

            # Our own sends come back through the same webhook. They are
            # recorded rather than dropped: a dispatcher may have replied to
            # the broker straight from Gmail, and the thread has to stay whole
            # for the agent to read it correctly.
            senders = message.get("from") or []
            sender = senders[0].get("email", "").lower() if senders else ""
            if sender == (account.email_address or "").lower():
                await inbound.handle_own_send(session, account, message)
                return

            await inbound.handle_inbound_message(session, account, message)
    except NylasError as e:
        logger.error(f"could not fetch message {message_id}, leaving it for retry: {e}")
    except Exception:
        logger.exception(f"unhandled error processing message {message_id}")
