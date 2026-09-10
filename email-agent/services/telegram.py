"""
Driver notifications. agent_bot owns the conversation, but it has no HTTP
server, so this service talks to the Bot API directly with the same token.
"""
import logging

import httpx

from config.settings import config

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


async def send_message(chat_id: str, text: str, bot_token: str | None = None) -> bool:
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
                    "parse_mode": "Markdown",
                },
            )
        except httpx.HTTPError as e:
            logger.error(f"telegram sendMessage to {chat_id} failed: {e}")
            return False

    if resp.status_code != 200:
        logger.error(f"telegram sendMessage to {chat_id} -> {resp.status_code}: {resp.text}")
        return False
    return True


def format_booked_notice(
    driver_name: str,
    pickup: str,
    delivery: str,
    pickup_date: str,
    driver_amount: float | None,
    load_number: str | None,
) -> str:
    rate_line = f"💰 Your rate: *${driver_amount:,.2f}*\n" if driver_amount else ""
    ref_line = f"📄 Load #: {load_number}\n" if load_number else ""
    return (
        f"✅ *Load booked!*\n\n"
        f"👤 {driver_name}\n"
        f"📍 Pick-up: {pickup}\n"
        f"📍 Delivery: {delivery}\n"
        f"📅 Pick-up date: {pickup_date}\n"
        f"{rate_line}"
        f"{ref_line}\n"
        f"The rate confirmation is in, and the load is on your board. "
        f"Your dispatcher will follow up with the details."
    )
