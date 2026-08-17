import logging
import httpx
from typing import Optional
from config.settings import config

logger = logging.getLogger(__name__)


class BoxManageService:
    def __init__(self):
        self.base_url = config.BOXTRUCK_BASE_URL
        self.headers  = {
            "X-Internal-Secret": config.BOXTRUCK_INTERNAL_SECRET,
            "Content-Type": "application/json",
        }

    async def get_driver_by_telegram_group(self, chat_id: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/hiring/drivers/",
                params={"telegram_group_id": chat_id},
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"boxmanage get_driver failed: {resp.status_code}")
                return None
            data = resp.json()
            results = data.get("results") or data
            if isinstance(results, list) and results:
                return results[0]
            return None

    async def update_driver_location(self, driver_id: int, zip_code: str) -> bool:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(
                f"{self.base_url}/hiring/drivers/{driver_id}/",
                json={"current_zip": zip_code},
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"boxmanage update_location failed: {resp.status_code} {resp.text}")
            return resp.status_code == 200

    async def get_nearby_drivers(self, zip_code: str, radius: float = 100) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{self.base_url}/hiring/drivers-nearby/",
                params={"zip": zip_code, "radius": radius},
                headers=self.headers,
            )
            if resp.status_code != 200:
                logger.error(f"boxmanage nearby failed: {resp.status_code}")
                return []
            return resp.json().get("drivers", [])

    async def list_active_drivers_with_telegram(self) -> list[dict]:
        """Every Active driver that has a linked Telegram group, across all pages."""
        drivers: list[dict] = []
        async with httpx.AsyncClient(timeout=15) as client:
            url = f"{self.base_url}/hiring/drivers/"
            params = {"page_size": 500}
            while url:
                resp = await client.get(url, params=params, headers=self.headers)
                if resp.status_code != 200:
                    logger.error(f"boxmanage list_active_drivers failed: {resp.status_code}")
                    break
                data = resp.json()
                results = data.get("results") if isinstance(data, dict) else data
                if isinstance(results, list):
                    drivers.extend(results)
                url = data.get("next") if isinstance(data, dict) else None
                params = None  # `next` already carries the full query string

        return [
            d for d in drivers
            if (d.get("status") or {}).get("name") == "Active" and d.get("telegram_group_id")
        ]

    async def busy_driver_ids(self) -> set[int]:
        """
        Drivers already running a load (Dispatched or In Transit in the TMS).

        Returns an empty set on any failure, matching the hold lookup: a driver
        offered one load too many beats the bot going silent.
        """
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.base_url}/billing/internal/busy-drivers/",
                    headers=self.headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"busy drivers unavailable, offering to everyone: {e}")
            return set()

        if resp.status_code != 200:
            logger.error(f"busy drivers returned {resp.status_code}, offering to everyone")
            return set()

        try:
            return {int(i) for i in resp.json().get("driver_ids", [])}
        except (ValueError, TypeError) as e:
            logger.error(f"could not read busy drivers: {e}")
            return set()


boxtruck = BoxManageService()
