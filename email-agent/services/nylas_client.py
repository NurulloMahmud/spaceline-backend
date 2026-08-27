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

    def hosted_auth_url(self, state: str, login_hint: str = "") -> str:
        """
        `access_type=offline` is what earns the refresh token; without it the
        grant stops working once the provider's first access token expires.

        `login_hint` preselects the mailbox on the provider's consent screen.
        It is a convenience, not a control — the user can still authorise a
        different account, which is why the callback verifies what came back.
        """
        from urllib.parse import urlencode

        params = {
            "client_id": config.NYLAS_CLIENT_ID,
            "redirect_uri": config.NYLAS_CALLBACK_URI,
            "response_type": "code",
            "access_type": "offline",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
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

    async def revoke_grant(self, grant_id: str) -> None:
        """
        Hands back a grant we decided not to keep. The callback creates a
        grant before it can see which mailbox was authorised, so rejecting a
        mismatch has to give up the access it just gained — otherwise we sit
        on a live connection to a mailbox we refused.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self.base_url}/v3/grants/{grant_id}",
                headers=self.headers,
            )
        # 404 means it is already gone, which is the state we wanted.
        if resp.status_code not in (200, 202, 204, 404):
            raise NylasError(f"grant revoke failed ({resp.status_code}): {resp.text[:400]}")

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
