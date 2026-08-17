"""
Manual check-in: asks every Active driver with a linked Telegram group for
their current zip code, so their location is fresh for load matching.

Run from the agent_bot directory with its venv active:
    python -m scripts.ask_zipcode
"""
import asyncio
import logging
import sys

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stderr)],
)

from telegram import Bot
from telegram.error import TelegramError

from config.settings import config
from services.boxmanage import boxtruck

logger = logging.getLogger(__name__)

MESSAGE = (
    "👋 Hello! This is your daily check-in.\n"
    "Please reply with your current zip code so we can match you with nearby loads."
)


async def main() -> None:
    drivers = await boxtruck.list_active_drivers_with_telegram()
    logger.info(f"Found {len(drivers)} active driver(s) with a linked Telegram group")

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    sent = 0
    for driver in drivers:
        chat_id = driver.get("telegram_group_id")
        try:
            await bot.send_message(chat_id=chat_id, text=MESSAGE)
            sent += 1
            logger.info(f"Sent check-in to driver {driver.get('id')} ({driver.get('full_name')})")
        except TelegramError as e:
            logger.error(
                f"Failed to message driver {driver.get('id')} ({driver.get('full_name')}): {e}"
            )

    logger.info(f"Check-in sent to {sent}/{len(drivers)} driver(s)")


if __name__ == "__main__":
    asyncio.run(main())
