"""
Driver notifications. agent_bot owns the conversation, but it has no HTTP
server, so this service talks to the Bot API directly with the same token.
"""
import logging

import httpx

from config.settings import config

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


async def send_message(
    chat_id: str, text: str, bot_token: str | None = None, parse_mode: str | None = "Markdown"
) -> bool:
    bot_token = bot_token if bot_token is not None else config.TELEGRAM_BOT_TOKEN
    if not bot_token:
        logger.error("no telegram bot token set — cannot send message")
        return False
    if not chat_id:
        logger.error("no telegram chat id — cannot send message")
        return False

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(
                f"{API_ROOT}/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    **({"parse_mode": parse_mode} if parse_mode else {}),
                },
            )
        except httpx.HTTPError as e:
            logger.error(f"telegram sendMessage to {chat_id} failed: {e}")
            return False

    if resp.status_code != 200:
        logger.error(f"telegram sendMessage to {chat_id} -> {resp.status_code}: {resp.text}")
        return False
    return True
