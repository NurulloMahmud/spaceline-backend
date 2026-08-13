"""Client for the atrek loads/bids service."""
import logging
from typing import Optional

import httpx

from config.settings import config

logger = logging.getLogger(__name__)


class AtrekService:
    def __init__(self):
        self.base_url = config.ATREK_BASE_URL
        self.headers = {
            "X-Internal-Secret": config.ATREK_INTERNAL_SECRET,
            "Content-Type": "application/json",
        }

    async def get_load(self, load_uuid: str) -> Optional[dict]:
        """Full third-party load detail: dims, contact email, notes."""
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/api/v1/loads/{load_uuid}",
                    headers=self.headers,
                )
            except httpx.HTTPError as e:
                logger.error(f"atrek get_load {load_uuid} failed: {e}")
                return None

        if resp.status_code != 200:
            logger.error(f"atrek get_load {load_uuid} -> {resp.status_code}")
            return None

        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    async def record_dispatcher_bid(
        self,
        load_uuid: str,
        company_id: int,
        user_id: int,
        driver_id: Optional[int],
        amount: float,
        driver_amount: Optional[float] = None,
        note: str = "",
    ) -> bool:
        payload = {
            "company_id": company_id,
            "user_id": user_id,
            "amount": amount,
            "note": note,
        }
        if driver_id is not None:
            payload["driver_id"] = driver_id
        if driver_amount is not None:
            payload["driver_amount"] = driver_amount

        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/api/v1/loads/{load_uuid}/bid",
                    json=payload,
                    headers=self.headers,
                )
            except httpx.HTTPError as e:
                logger.error(f"atrek record_bid {load_uuid} failed: {e}")
                return False

        if resp.status_code != 201:
            logger.error(f"atrek record_bid {load_uuid} -> {resp.status_code}: {resp.text}")
        return resp.status_code == 201


atrek = AtrekService()
