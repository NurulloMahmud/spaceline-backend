import json
import redis.asyncio as redis
from typing import Optional
from config.settings import config


class StateService:

    def __init__(self):
        self.redis = redis.from_url(config.REDIS_URL, decode_responses=True)


    async def set_pending_offer(self, driver_id: int, load: dict, ttl: int = 3600):
        await self.redis.setex(
            f"offer:{driver_id}",
            ttl,
            json.dumps(load),
        )

    async def get_pending_offer(self, driver_id: int) -> Optional[dict]:
        """Get the current pending load offer for a driver."""
        raw = await self.redis.get(f"offer:{driver_id}")
        return json.loads(raw) if raw else None

    async def clear_pending_offer(self, driver_id: int):
        await self.redis.delete(f"offer:{driver_id}")


    async def cache_driver(self, chat_id: str, driver: dict, ttl: int = 86400):
        await self.redis.setex(
            f"driver:chat:{chat_id}",
            ttl,
            json.dumps(driver),
        )

    async def get_cached_driver(self, chat_id: str) -> Optional[dict]:
        raw = await self.redis.get(f"driver:chat:{chat_id}")
        return json.loads(raw) if raw else None


    async def set_state(self, driver_id: int, state: str, ttl: int = 3600):
        """Set conversation state for a driver."""
        await self.redis.setex(f"state:{driver_id}", ttl, state)

    async def get_state(self, driver_id: int) -> Optional[str]:
        return await self.redis.get(f"state:{driver_id}")

    async def clear_state(self, driver_id: int):
        await self.redis.delete(f"state:{driver_id}")


state = StateService()