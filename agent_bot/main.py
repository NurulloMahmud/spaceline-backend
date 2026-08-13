import asyncio
import logging
import sys

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stderr),
    ]
)

from telegram.ext import Application, MessageHandler, filters, ChatMemberHandler as TGChatMemberHandler

from config.settings import config
from database.connection import init_db
from database.repository import DriverPreferenceRepo, PendingOfferRepo
from services.agent import agent
from services.boxmanage import boxtruck
from services.ai import ai
from state.cache import driver_cache
from agents.location import LocationAgent
from agents.preference import PreferenceAgent
from agents.loads import LoadAgent
import bot.handlers as handlers

logger = logging.getLogger(__name__)

async def listen_loads(bot_instance) -> None:
    logger.info("Starting atrek SSE listener...")
    while True:
        try:
            async for event in agent.stream_loads():
                event_type = event.get("type")
                if event_type == "LOAD_CREATED":
                    load_data = event.get("data", {})
                    # The event's own id is the loads service's UUID; the id
                    # inside `data` is the source feed's numeric id. Only the
                    # former works against the detail and bid endpoints.
                    await handlers.load_agent.on_new_load(
                        load_data, bot_instance, event.get("id")
                    )
        except Exception as e:
            logger.error(f"SSE stream error: {e} — reconnecting in 5s")
            await asyncio.sleep(5)


async def expire_offers_loop(offer_repo: PendingOfferRepo) -> None:
    while True:
        try:
            offer_repo.expire_old()
        except Exception as e:
            logger.error(f"Expire offers error: {e}")
        await asyncio.sleep(60)


async def main() -> None:
    logger.info("Initializing database...")
    init_db()
    logger.info("Database ready")

    pref_repo  = DriverPreferenceRepo()
    offer_repo = PendingOfferRepo()

    handlers.location_agent   = LocationAgent()
    handlers.preference_agent = PreferenceAgent(pref_repo)
    handlers.load_agent       = LoadAgent(pref_repo, offer_repo)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        MessageHandler(filters.TEXT, handlers.on_message)
    )
    app.add_handler(
        TGChatMemberHandler(
            handlers.on_bot_added_to_group,
            TGChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    logger.info("Starting telegram-agent...")
    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=[
                "message",
                "my_chat_member",
                "chat_member",
            ]
        )

        logger.info("Bot is running. Listening for messages and loads...")

        await asyncio.gather(
            listen_loads(app.bot),
            expire_offers_loop(offer_repo),
        )

        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
