"""
Drivers post their zipcode in the group every day and the bot saves it as
their current location — this is the only location source (the mobile app was
cancelled). The reply-only routing must not break this daily check-in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://localhost/unused")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

import pytest  # noqa: E402

from agents.location import BARE_ZIP, LocationAgent  # noqa: E402
from agents import location as location_module  # noqa: E402


def test_bare_zip_is_recognised():
    assert BARE_ZIP.match("75201").group(1) == "75201"
    assert BARE_ZIP.match("  75201  ").group(1) == "75201"
    assert BARE_ZIP.match("75201-1234").group(1) == "75201"


def test_non_zip_text_is_not_a_bare_zip():
    for text in ("@mr_makhammatoff", "yes", "1200", "on my way", "752011", "load 75201 ready"):
        assert BARE_ZIP.match(text) is None


@pytest.fixture
def location_agent(monkeypatch):
    calls = {"update": [], "is_location": 0, "extract": 0}

    async def fake_get_driver(chat_id):
        return {"id": 42, "full_name": "Bobur"}

    async def fake_update(driver_id, zip_code):
        calls["update"].append((driver_id, zip_code))
        return True

    def fake_is_location(message):
        calls["is_location"] += 1
        return False

    def fake_extract(message):
        calls["extract"] += 1
        return None

    async def fake_cache_get(chat_id):
        return None

    async def fake_cache_set(chat_id, driver):
        pass

    monkeypatch.setattr(location_module.boxtruck, "get_driver_by_telegram_group", fake_get_driver)
    monkeypatch.setattr(location_module.boxtruck, "update_driver_location", fake_update)
    monkeypatch.setattr(location_module.ai, "is_location_message", fake_is_location)
    monkeypatch.setattr(location_module.ai, "extract_location", fake_extract)
    monkeypatch.setattr(location_module.driver_cache, "get", fake_cache_get)
    monkeypatch.setattr(location_module.driver_cache, "set", fake_cache_set)
    return LocationAgent(), calls


async def test_daily_bare_zip_updates_location_without_the_model(location_agent):
    agent, calls = location_agent

    reply = await agent.handle("75201", "-100123")

    assert calls["update"] == [(42, "75201")]
    assert calls["is_location"] == 0, "a bare zip must not need an AI call"
    assert "75201" in reply


async def test_chatter_does_not_update_location(location_agent):
    agent, calls = location_agent

    reply = await agent.handle("@mr_makhammatoff", "-100123")

    assert reply is None
    assert calls["update"] == []


async def test_location_sentence_falls_back_to_the_model(location_agent, monkeypatch):
    agent, calls = location_agent
    monkeypatch.setattr(location_module.ai, "is_location_message", lambda m: True)
    monkeypatch.setattr(
        location_module.ai, "extract_location",
        lambda m: {"zip": "60609", "city": "Chicago", "state": "IL"},
    )

    reply = await agent.handle("I'm in Chicago 60609 now", "-100123")

    assert calls["update"] == [(42, "60609")]
    assert "Chicago" in reply
