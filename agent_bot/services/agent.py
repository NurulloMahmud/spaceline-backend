import json
import logging
import httpx
from typing import AsyncGenerator, Optional
from config.settings import config

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self):
        self.base_url = config.AGENT_BASE_URL
        self.headers  = {
            "X-Internal-Secret": config.AGENT_INTERNAL_SECRET,
            "Content-Type": "application/json",
        }

    async def stream_loads(self) -> AsyncGenerator[dict, None]:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/loads/stream",
                headers={**self.headers, "Accept": "text/event-stream"},
            ) as response:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue

    async def get_load(self, load_id: str) -> Optional[dict]:
        """Full load detail from the third-party API (includes dims, pieces, weight)."""
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/loads/{load_id}",
                    headers=self.headers,
                )
            except httpx.HTTPError as e:
                logger.error(f"get_load {load_id} failed: {e}")
                return None

            if resp.status_code != 200:
                logger.error(f"get_load {load_id} returned {resp.status_code}")
                return None

            body = resp.json()
            return body.get("data") if isinstance(body, dict) and "data" in body else body

    async def place_bid(
        self,
        load_id: str,
        company_id: int,
        driver_id: int,
        amount: float,
        driver_amount: Optional[float] = None,
        note: str = "",
    ) -> bool:
        async with httpx.AsyncClient(timeout=10) as client:
            payload = {
                "driver_id": driver_id,
                "company_id": company_id,
                "amount": amount,
                "note": note,
            }
            if driver_amount is not None:
                payload["driver_amount"] = driver_amount

            try:
                resp = await client.post(
                    f"{self.base_url}/loads/{load_id}/bid",
                    json=payload,
                    headers=self.headers,
                )
            except httpx.HTTPError as e:
                logger.error(f"place_bid load={load_id} driver={driver_id} failed: {e}")
                return False

            if resp.status_code != 201:
                logger.error(
                    f"place_bid load={load_id} driver={driver_id} "
                    f"returned {resp.status_code}: {resp.text}"
                )
            return resp.status_code == 201


agent = AgentService()
