import json
import logging
import redis.asyncio as redis
from typing import Optional
from config.settings import config

logger = logging.getLogger(__name__)


class DriverCache:

    def __init__(self):
        self.redis = redis.from_url(config.REDIS_URL, decode_responses=True)

    async def get(self, chat_id: str) -> Optional[dict]:
        try:
            raw = await self.redis.get(f"driver:chat:{chat_id}")
            return json.loads(raw) if raw else None
        except Exception as e:
            logger.warning(f"Redis get failed: {e}")
            return None

    async def set(self, chat_id: str, driver: dict, ttl: int = 86400):
        try:
            await self.redis.setex(
                f"driver:chat:{chat_id}",
                ttl,
                json.dumps(driver),
            )
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")

    async def invalidate(self, chat_id: str):
        try:
            await self.redis.delete(f"driver:chat:{chat_id}")
        except Exception as e:
            logger.warning(f"Redis delete failed: {e}")


driver_cache = DriverCache()
