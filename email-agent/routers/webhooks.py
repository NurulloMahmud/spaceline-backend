"""
Nylas webhook receiver.

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
    if trigger not in MESSAGE_TRIGGERS:
        return JSONResponse({"ok": True, "ignored": trigger})

    data = (payload.get("data") or {}).get("object") or {}
    grant_id = data.get("grant_id") or (payload.get("data") or {}).get("grant_id")
    message_id = data.get("id")

    if not grant_id or not message_id:
        logger.warning(f"webhook {trigger} missing grant or message id")
        return JSONResponse({"ok": True, "ignored": "incomplete"})

    background.add_task(process_message, grant_id, message_id)
    return JSONResponse({"ok": True})


async def process_message(grant_id: str, message_id: str) -> None:
    """Runs after the webhook response. Every failure is logged, never raised."""
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
            session.add(models.ProcessedWebhook(nylas_message_id=message_id))
            session.flush()

            try:
                message = await nylas.get_message(grant_id, message_id)
            except NylasError as e:
                logger.error(f"could not fetch message {message_id}: {e}")
                return

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
    except Exception:
        logger.exception(f"unhandled error processing message {message_id}")
