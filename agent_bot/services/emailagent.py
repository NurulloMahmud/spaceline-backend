import logging
import httpx
from config.settings import config

logger = logging.getLogger(__name__)


class EmailAgentService:
    """Read-only view of which drivers currently have a bid live with a broker."""

    def __init__(self):
        self.base_url = (config.EMAIL_AGENT_BASE_URL or "").rstrip("/")
        self.headers = {
            "X-Internal-Secret": config.EMAIL_AGENT_INTERNAL_SECRET,
            "Content-Type": "application/json",
        }

    async def held_driver_ids(self) -> set[int]:
        """
        Drivers to skip when matching a load.

        On any failure this returns an empty set: a driver still being offered
        loads is a far smaller problem than the bot going quiet because one
        service is down.
        """
        if not self.base_url:
            return set()

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.base_url}/internal/v1/driver-holds",
                    headers=self.headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"driver holds unavailable, offering to everyone: {e}")
            return set()

        if resp.status_code != 200:
            logger.error(
                f"driver holds returned {resp.status_code}, offering to everyone"
            )
            return set()

        try:
            return {int(i) for i in resp.json().get("driver_ids", [])}
        except (ValueError, TypeError) as e:
            logger.error(f"could not read driver holds: {e}")
            return set()


emailagent = EmailAgentService()
