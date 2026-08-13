import logging
import re
from typing import Optional
from services.ai import ai
from services.boxmanage import boxtruck
from state.cache import driver_cache

logger = logging.getLogger(__name__)

# A bare 5-digit zip is the common daily check-in ("75201"); handle it without
# asking the model. \b keeps it from matching inside longer numbers.
BARE_ZIP = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")


class LocationAgent:

    async def handle(self, message: str, chat_id: str) -> Optional[str]:
        bare = BARE_ZIP.match(message)
        if bare:
            location = {"zip": bare.group(1), "city": "", "state": ""}
        else:
            if not ai.is_location_message(message):
                return None
            location = ai.extract_location(message)

        if not location or not location.get("zip"):
            return None

        driver = await driver_cache.get(chat_id)
        if not driver:
            driver = await boxtruck.get_driver_by_telegram_group(chat_id)
            if not driver:
                logger.warning(f"No driver for chat_id={chat_id}")
                return None
            await driver_cache.set(chat_id, driver)

        driver_id = driver["id"]
        zip_code  = location["zip"]
        city      = location.get("city", "")
        state     = location.get("state", "")

        success = await boxtruck.update_driver_location(driver_id, zip_code)

        if success:
            logger.info(f"Driver {driver_id} location updated to {zip_code}")
            return f"✅ Location updated: {city}, {state} {zip_code}".strip()

        logger.error(f"Failed to update driver {driver_id} location")
        return None


location_agent = LocationAgent()
