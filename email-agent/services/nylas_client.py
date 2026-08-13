"""
Nylas v3 wrapper. Uses the REST API over httpx rather than the SDK so every
call stays async and the failure surface is a single place.
"""
import hashlib
import hmac
import logging
from typing import Any, Optional

import httpx

from config.settings import config

logger = logging.getLogger(__name__)


class NylasError(Exception):
    pass


class NylasService:
    def __init__(self):
        self.base_url = config.NYLAS_API_URI.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {config.NYLAS_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --- auth -----------------------------------------------------------

    def hosted_auth_url(self, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": config.NYLAS_CLIENT_ID,
            "redirect_uri": config.NYLAS_CALLBACK_URI,
            "response_type": "code",
            "access_type": "offline",
            "state": state,
        }
        return f"{self.base_url}/v3/connect/auth?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.base_url}/v3/connect/token",
                json={
                    "client_id": config.NYLAS_CLIENT_ID,
                    "client_secret": config.NYLAS_API_KEY,
                    "redirect_uri": config.NYLAS_CALLBACK_URI,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code not in (200, 201):
            raise NylasError(f"code exchange failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json()

    # --- messages -------------------------------------------------------

    async def send_message(
        self,
        grant_id: str,
        to_email: str,
        subject: str,
        body: str,
        reply_to_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "to": [{"email": to_email}],
            "subject": subject,
            "body": body,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.base_url}/v3/grants/{grant_id}/messages/send",
                json=payload,
                headers=self.headers,
            )
        if resp.status_code not in (200, 201):
            raise NylasError(f"send failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json().get("data", {})

    async def get_message(self, grant_id: str, message_id: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self.base_url}/v3/grants/{grant_id}/messages/{message_id}",
                headers=self.headers,
            )
        if resp.status_code != 200:
            raise NylasError(f"get message failed ({resp.status_code}): {resp.text[:400]}")
        return resp.json().get("data", {})

    async def download_attachment(
        self, grant_id: str, attachment_id: str, message_id: str
    ) -> bytes:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(
                f"{self.base_url}/v3/grants/{grant_id}/attachments/{attachment_id}/download",
                params={"message_id": message_id},
                headers={"Authorization": f"Bearer {config.NYLAS_API_KEY}"},
            )
        if resp.status_code != 200:
            raise NylasError(f"attachment download failed ({resp.status_code})")
        return resp.content

    # --- webhooks -------------------------------------------------------

    @staticmethod
    def verify_webhook(signature: str, raw_body: bytes) -> bool:
        if not config.NYLAS_WEBHOOK_SECRET:
            logger.error("NYLAS_WEBHOOK_SECRET is not set — rejecting webhook")
            return False
        if not signature:
            return False
        expected = hmac.new(
            config.NYLAS_WEBHOOK_SECRET.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


nylas = NylasService()
