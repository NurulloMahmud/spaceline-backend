"""Client for the TMS. All calls use the shared internal secret."""
import logging
from typing import Any, Optional

import httpx

from config.settings import config

logger = logging.getLogger(__name__)


class BoxTruckError(Exception):
    """A TMS call failed in a way the dispatcher needs to hear about."""


class BoxTruckService:
    def __init__(self):
        self.base_url = config.BOXTRUCK_BASE_URL
        self.headers = {"X-Internal-Secret": config.BOXTRUCK_INTERNAL_SECRET}

    def _json_headers(self) -> dict:
        return {**self.headers, "Content-Type": "application/json"}

    async def get_company(self, company_id: int) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/billing/internal/company/{company_id}/",
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"get_company {company_id} -> {resp.status_code}")
                return None
            return resp.json()

    async def get_dispatcher(self, user_id: int) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/billing/internal/dispatcher/{user_id}/",
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"get_dispatcher {user_id} -> {resp.status_code}")
                return None
            return resp.json()

    async def get_driver(self, driver_id: int) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/hiring/drivers/{driver_id}/",
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"get_driver {driver_id} -> {resp.status_code}")
                return None
            return resp.json()

    async def get_drivers_bulk(self, ids: list[int]) -> dict[int, dict]:
        """Batched driver lookup — one call for every distinct driver_id in a list response."""
        if not ids:
            return {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/hiring/drivers-bulk/",
                params={"ids": ",".join(str(i) for i in ids)},
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"get_drivers_bulk -> {resp.status_code}")
                return {}
            return {d["id"]: d for d in resp.json()}

    async def get_dispatchers_bulk(self, ids: list[int]) -> dict[int, dict]:
        """Batched dispatcher lookup — one call for every distinct dispatcher_user_id in a list response."""
        if not ids:
            return {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/api/users/users-bulk/",
                params={"ids": ",".join(str(i) for i in ids)},
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"get_dispatchers_bulk -> {resp.status_code}")
                return {}
            return {u["id"]: u for u in resp.json()}

    async def resolve_broker(
        self, name: str = "", mc: str = "", email: str = ""
    ) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self.base_url}/api/billing/internal/brokers/resolve/",
                json={"name": name, "mc": mc, "email": email},
                headers=self._json_headers(),
            )
            if resp.status_code not in (200, 201):
                logger.error(f"resolve_broker -> {resp.status_code}: {resp.text}")
                return None
            return resp.json()

    async def parse_ratecon(
        self, filename: str, content: bytes, broker_id: Optional[int] = None
    ) -> dict:
        """
        Returns the parsed ratecon. Raises BoxTruckError when the document
        could not be read — the caller must surface that to dispatch.
        """
        data = {"broker": str(broker_id)} if broker_id else {}
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{self.base_url}/api/billing/internal/parse-ratecon/",
                files={"file": (filename, content, "application/pdf")},
                data=data,
                headers=self.headers,
            )
        if resp.status_code != 200:
            raise BoxTruckError(f"ratecon parse failed ({resp.status_code}): {resp.text[:500]}")

        parsed = resp.json().get("parsed_data")
        if not parsed:
            raise BoxTruckError("ratecon parse returned no data")
        return parsed

    async def book_load(
        self,
        payload: dict[str, Any],
        ratecon: Optional[tuple[str, bytes]] = None,
    ) -> dict:
        """Create the load, its stops and its ratecon file in one transaction."""
        import json

        files = {}
        if ratecon:
            files["ratecon"] = (ratecon[0], ratecon[1], "application/pdf")

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/api/billing/internal/book-load/",
                data={"payload": json.dumps(payload)},
                files=files or None,
                headers=self.headers,
            )
        if resp.status_code not in (200, 201):
            raise BoxTruckError(f"book-load failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()


boxtruck = BoxTruckService()
