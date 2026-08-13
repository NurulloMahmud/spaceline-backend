"""
The message router has exactly two jobs: send a reply-to-an-offer to bid
handling, and send a standalone message to location capture. Everything else
is ignored — no message ever reaches the bid classifier unless it is a reply
to one of our offers.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://localhost/unused")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest  # noqa: E402

import bot.handlers as handlers  # noqa: E402

BOT_ID = 999
OFFER_MESSAGE_ID = 2394


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self, text, reply_to=None, chat_id=-100123):
        self.text = text
        self.reply_to_message = reply_to
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, parse_mode=None):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, message):
        self.message = message


class FakeBot:
    id = BOT_ID


class FakeContext:
    bot = FakeBot()


class FakeOfferRepo:
    def __init__(self, offer=None):
        self.offer = offer

    def get_by_message_id(self, message_id):
        if self.offer and message_id == OFFER_MESSAGE_ID:
            return self.offer
        return None


class FakeLoadAgent:
    def __init__(self, offer=None):
        self.offer_repo = FakeOfferRepo(offer)
        self.offer_reply_calls = []

    async def handle_offer_reply(self, message, driver, offer):
        self.offer_reply_calls.append((message, offer))
        return "✅ Got your rate"


class FakeLocationAgent:
    def __init__(self):
        self.calls = []

    async def handle(self, message, chat_id):
        self.calls.append((message, chat_id))
        return "✅ Location updated: 75201" if message.strip() == "75201" else None


@pytest.fixture
def wired(monkeypatch):
    offer = type("Offer", (), {"id": 1, "driver_id": 42, "load_id": "uuid"})()
    load_agent = FakeLoadAgent(offer)
    location_agent = FakeLocationAgent()

    async def fake_get_driver(chat_id):
        return {"id": 42, "full_name": "Bobur"}

    monkeypatch.setattr(handlers, "load_agent", load_agent)
    monkeypatch.setattr(handlers, "location_agent", location_agent)
    monkeypatch.setattr(handlers.boxtruck, "get_driver_by_telegram_group", fake_get_driver)
    return load_agent, location_agent


async def test_reply_to_our_offer_goes_to_bid_handling(wired):
    load_agent, location_agent = wired
    bot_msg = FakeMessage("$1200", reply_to=FakeMessage("offer", chat_id=None))
    bot_msg.reply_to_message.from_user = FakeUser(BOT_ID)
    bot_msg.reply_to_message.message_id = OFFER_MESSAGE_ID
    update = FakeUpdate(bot_msg)

    await handlers.on_message(update, FakeContext())

    assert len(load_agent.offer_reply_calls) == 1
    assert load_agent.offer_reply_calls[0][0] == "$1200"
    assert location_agent.calls == []
    assert bot_msg.replies == ["✅ Got your rate"]


async def test_standalone_zip_goes_to_location(wired):
    load_agent, location_agent = wired
    update = FakeUpdate(FakeMessage("75201"))

    await handlers.on_message(update, FakeContext())

    assert location_agent.calls == [("75201", "-100123")]
    assert load_agent.offer_reply_calls == [], "a non-reply must never reach bid handling"


async def test_plain_chatter_reaches_neither_as_a_bid(wired):
    load_agent, location_agent = wired
    update = FakeUpdate(FakeMessage("@mr_makhammatoff"))

    await handlers.on_message(update, FakeContext())

    assert load_agent.offer_reply_calls == []
    # It is offered to location, which no-ops on non-zip text.
    assert location_agent.calls == [("@mr_makhammatoff", "-100123")]
    assert update.message.replies == []


async def test_reply_to_a_non_offer_message_is_ignored(wired):
    load_agent, location_agent = wired
    # A reply to one of our messages that is not an offer (e.g. a booking notice).
    bot_msg = FakeMessage("thanks!", reply_to=FakeMessage("Load booked!", chat_id=None))
    bot_msg.reply_to_message.from_user = FakeUser(BOT_ID)
    bot_msg.reply_to_message.message_id = 7777  # not an offer id
    update = FakeUpdate(bot_msg)

    await handlers.on_message(update, FakeContext())

    assert load_agent.offer_reply_calls == []
    # Falls through to location, which no-ops.
    assert location_agent.calls == [("thanks!", "-100123")]


async def test_reply_to_another_user_is_not_a_bid(wired):
    load_agent, location_agent = wired
    bot_msg = FakeMessage("go 1000", reply_to=FakeMessage("some driver chatter", chat_id=None))
    bot_msg.reply_to_message.from_user = FakeUser(12345)  # another person, not the bot
    bot_msg.reply_to_message.message_id = OFFER_MESSAGE_ID
    update = FakeUpdate(bot_msg)

    await handlers.on_message(update, FakeContext())

    assert load_agent.offer_reply_calls == [], "replying to a person is not bidding"
