"""
Two ids describe the same load: the source feed's numeric id (inside the event
`data`) and the loads service's UUID (the event's own `id`). Only the UUID
works against the detail and bid endpoints, and mixing them up silently costs
dimensions on every offer and every driver bid.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The engine and the OpenAI client are built at import time. SQLAlchemy
# connects lazily and neither is exercised here — the repos and AI calls are
# replaced with fakes.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://localhost/unused")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest  # noqa: E402

from agents import loads as loads_module  # noqa: E402

LOAD_UUID = "3d7cee8e-d40f-4f4f-a0b4-5d44e5551bbd"
SOURCE_ID = 2097139

FEED_PAYLOAD = {
    "id": SOURCE_ID,
    "pick_up_at": "Chicago, IL 60609",
    "pick_up_zip": "60609",
    "pick_up_latitude": 41.8,
    "pick_up_longitude": -87.65,
    "deliver_to": "Lincoln, NE 68508",
    "vehicle_type": "CARGO VAN",
    "suggested_truck": "CARGO VAN",
    "miles": 532,
}

DETAIL_PAYLOAD = {"dims": [77, 14, 75], "pieces": 2, "weight": 500}

DRIVER = {
    "id": 42,
    "full_name": "Bobur Mahammatov",
    "telegram_group_id": "-1001234567890",
    "current_latitude": 41.9,
    "current_longitude": -87.9,
    "company": {"id": 1},
    "vehicle": {"vehicle_type": "CARGO VAN", "length": 190, "width": 70, "height": 76, "payload": 5000},
}


class SentMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []
        self.next_message_id = 5000

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        self.next_message_id += 1
        return SentMessage(self.next_message_id)


class FakeOfferRepo:
    def __init__(self):
        self.created = []
        self.active = None
        self.statuses = []

    def create(self, driver_id, load_id, load_snapshot, expires_at,
               telegram_message_id=None):
        offer = type("Offer", (), {
            "id": 1, "driver_id": driver_id, "load_id": load_id,
            "load_snapshot": load_snapshot,
            "telegram_message_id": telegram_message_id,
        })()
        self.created.append(offer)
        return offer

    def get_active(self, driver_id):
        return self.active

    def get_by_message_id(self, message_id):
        for offer in self.created:
            if offer.telegram_message_id == message_id:
                return offer
        return None

    def update_status(self, offer_id, status):
        self.statuses.append((offer_id, status))


def make_offer(load_id=LOAD_UUID, driver_id=42, offer_id=1):
    return type("Offer", (), {
        "id": offer_id, "load_id": load_id, "driver_id": driver_id,
    })()


class FakePrefRepo:
    def get_effective(self, driver_id):
        return None


@pytest.fixture
def agent_calls(monkeypatch):
    calls = {"get_load": [], "place_bid": []}

    async def fake_get_load(load_id):
        calls["get_load"].append(load_id)
        return DETAIL_PAYLOAD if load_id == LOAD_UUID else None

    async def fake_place_bid(**kwargs):
        calls["place_bid"].append(kwargs)
        return True

    async def fake_nearby(zip_code, radius=50):
        return [DRIVER]

    monkeypatch.setattr(loads_module.agent, "get_load", fake_get_load)
    monkeypatch.setattr(loads_module.agent, "place_bid", fake_place_bid)
    monkeypatch.setattr(loads_module.boxtruck, "get_nearby_drivers", fake_nearby)
    return calls


@pytest.fixture
def agent_under_test():
    return loads_module.LoadAgent(FakePrefRepo(), FakeOfferRepo())


async def test_detail_is_fetched_with_the_service_uuid(agent_under_test, agent_calls):
    bot = FakeBot()
    await agent_under_test.on_new_load(dict(FEED_PAYLOAD), bot, LOAD_UUID)

    assert agent_calls["get_load"] == [LOAD_UUID], (
        "the source feed's numeric id 404s against the detail endpoint"
    )
    assert str(SOURCE_ID) not in agent_calls["get_load"]


async def test_offer_message_carries_the_dimensions(agent_under_test, agent_calls):
    bot = FakeBot()
    await agent_under_test.on_new_load(dict(FEED_PAYLOAD), bot, LOAD_UUID)

    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "Dimensions: 77\" × 14\" × 75\"" in text
    assert "Pieces: 2" in text
    assert "Weight: 500 lbs" in text


async def test_offer_is_recorded_against_the_service_uuid(agent_under_test, agent_calls):
    await agent_under_test.on_new_load(dict(FEED_PAYLOAD), FakeBot(), LOAD_UUID)

    offer = agent_under_test.offer_repo.created[0]
    assert offer.load_id == LOAD_UUID, (
        "the bid endpoint rejects anything that is not a UUID"
    )


async def test_a_driver_bid_is_placed_against_the_service_uuid(
    agent_under_test, agent_calls, monkeypatch
):
    monkeypatch.setattr(
        loads_module.ai, "classify_offer_reply",
        lambda message: {"intent": "bid", "amount": 1200.0},
    )

    reply = await agent_under_test.handle_offer_reply("$1200", DRIVER, make_offer())

    assert agent_calls["place_bid"], "the bid never reached the loads service"
    bid = agent_calls["place_bid"][0]
    assert bid["load_id"] == LOAD_UUID
    assert bid["company_id"] == 1
    assert bid["driver_id"] == 42
    assert bid["amount"] == 1200.0
    assert "Got your rate" in reply


async def test_a_failed_bid_does_not_confirm_to_the_driver(
    agent_under_test, agent_calls, monkeypatch
):
    async def failing_bid(**kwargs):
        return False

    monkeypatch.setattr(loads_module.agent, "place_bid", failing_bid)
    monkeypatch.setattr(
        loads_module.ai, "classify_offer_reply",
        lambda message: {"intent": "bid", "amount": 900.0},
    )

    reply = await agent_under_test.handle_offer_reply("$900", DRIVER, make_offer())

    assert "couldn't reach dispatch" in reply
    assert agent_under_test.offer_repo.statuses == [], (
        "an unrecorded bid must not be marked accepted"
    )


@pytest.mark.parametrize("intent", ["other", "decline"])
async def test_non_bid_replies_are_left_alone(
    agent_under_test, agent_calls, monkeypatch, intent
):
    """Chatter and declines must not place bids, cancel offers, or get answered."""
    monkeypatch.setattr(
        loads_module.ai, "classify_offer_reply",
        lambda message: {"intent": intent, "amount": None},
    )

    reply = await agent_under_test.handle_offer_reply(
        "@mr_makhammatoff", DRIVER, make_offer()
    )

    assert reply is None
    assert agent_calls["place_bid"] == []
    assert agent_under_test.offer_repo.statuses == []


async def test_interest_without_a_price_asks_for_one(
    agent_under_test, agent_calls, monkeypatch
):
    monkeypatch.setattr(
        loads_module.ai, "classify_offer_reply",
        lambda message: {"intent": "interested", "amount": None},
    )

    reply = await agent_under_test.handle_offer_reply("yes", DRIVER, make_offer())

    assert "What rate" in reply
    assert agent_calls["place_bid"] == []


async def test_an_unclassifiable_reply_does_nothing(
    agent_under_test, agent_calls, monkeypatch
):
    """A model failure must not be read as a bid or a decline."""
    monkeypatch.setattr(loads_module.ai, "classify_offer_reply", lambda message: None)

    reply = await agent_under_test.handle_offer_reply("...", DRIVER, make_offer())

    assert reply is None
    assert agent_calls["place_bid"] == []
    assert agent_under_test.offer_repo.statuses == []


async def test_offer_records_the_message_it_was_sent_as(agent_under_test, agent_calls):
    """The reply-to id is how a bid is tied back to its offer."""
    bot = FakeBot()
    await agent_under_test.on_new_load(dict(FEED_PAYLOAD), bot, LOAD_UUID)

    offer = agent_under_test.offer_repo.created[0]
    assert offer.telegram_message_id == 5001
    assert agent_under_test.offer_repo.get_by_message_id(5001) is offer
    assert agent_under_test.offer_repo.get_by_message_id(9999) is None


async def test_a_missing_uuid_still_sends_the_offer(agent_under_test, agent_calls):
    """Degraded, but the driver should still see the load."""
    bot = FakeBot()
    await agent_under_test.on_new_load(dict(FEED_PAYLOAD), bot, None)

    assert agent_calls["get_load"] == []
    assert len(bot.sent) == 1
    assert "Dimensions" not in bot.sent[0]["text"]


def test_dimensions_line_is_omitted_when_unknown():
    assert loads_module.format_dims({}) is None
    assert loads_module.format_dims({"dims": [0, 0, 0]}) is None
    assert loads_module.format_dims({"dims": [96, 48, 60]}) == '96" × 48" × 60"'
