import logging
from telegram import Update
from telegram.ext import ContextTypes
from state.cache import driver_cache
from services.boxmanage import boxtruck

logger = logging.getLogger(__name__)

location_agent   = None
preference_agent = None
load_agent       = None


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Driver groups are working chat rooms, so the bot acts on only two things:

      1. a telegram reply to one of its own load offers — treated as a bid;
      2. a standalone message that contains a zipcode — the driver's daily
         location check-in.

    Everything else is left alone. In particular, the bid classifier only ever
    runs on a reply to an offer, so ordinary chatter can never be read as a
    bid or a decline.
    """
    if not update.message or not update.message.text:
        return

    message = update.message.text
    chat_id = str(update.message.chat_id)

    offer = await _matched_offer(update, context)
    if offer:
        await _handle_offer_reply(update, chat_id, message, offer)
        return

    # Not a reply to an offer — the only other thing we care about is a driver
    # posting their location. location_agent no-ops unless it finds a zipcode,
    # so chatter falls through silently.
    reply = await location_agent.handle(message, chat_id)
    if reply:
        logger.info(f"[LOCATION] chat_id={chat_id} -> {reply}")
        await update.message.reply_text(reply)


async def _matched_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The active offer this message is a reply to, or None."""
    replied_to = update.message.reply_to_message
    if not replied_to:
        return None
    if not replied_to.from_user or replied_to.from_user.id != context.bot.id:
        return None
    return load_agent.offer_repo.get_by_message_id(replied_to.message_id)


async def _handle_offer_reply(update, chat_id: str, message: str, offer) -> None:
    logger.info(f"[OFFER REPLY] chat_id={chat_id} offer={offer.id} text='{message}'")

    driver = await boxtruck.get_driver_by_telegram_group(chat_id)
    if not driver:
        logger.warning(f"[SKIP] no driver for chat_id={chat_id}")
        return

    if driver["id"] != offer.driver_id:
        logger.warning(
            f"[SKIP] offer {offer.id} belongs to driver {offer.driver_id}, "
            f"not {driver['id']}"
        )
        return

    reply = await load_agent.handle_offer_reply(message, driver, offer)
    logger.info(f"[OFFER REPLY] result={reply}")
    if reply:
        await update.message.reply_text(reply, parse_mode="Markdown")

async def on_bot_added_to_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = update.my_chat_member
    if not result:
        return

    new_status = result.new_chat_member.status
    if new_status not in ("member", "administrator"):
        return

    chat_id = str(result.chat.id)

    driver = await boxtruck.get_driver_by_telegram_group(chat_id)
    if driver:
        await driver_cache.set(chat_id, driver)
        await context.bot.send_message(
            chat_id,
            f"✅ Connected to driver: *{driver['full_name']}*\n"
            f"I'll send you load offers and track your location.",
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id,
            f"👋 Hi! I'm your load assistant bot.\n\n"
            f"This group is not linked to a driver yet.\n"
            f"Please give this Group ID to your dispatcher:\n\n"
            f"`{chat_id}`",
            parse_mode="Markdown",
        )
