"""The Telegram IP alert names the user, not only the id."""
from routers import events as events_router
from services.auth import Principal
from services.boxtruck import boxtruck


def _capture(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(chat_id, text, bot_token=None):
        sent.append(text)
        return True

    monkeypatch.setattr(events_router.telegram, "send_message", fake_send)
    monkeypatch.setattr(events_router, "_user_labels", {})
    return sent


async def test_alert_includes_username_and_name(monkeypatch) -> None:
    sent = _capture(monkeypatch)
    calls: list[list[int]] = []

    async def fake_bulk(ids):
        calls.append(ids)
        return {7: {"id": 7, "username": "jane", "first_name": "Jane", "last_name": "Doe"}}

    monkeypatch.setattr(boxtruck, "get_dispatchers_bulk", fake_bulk)
    principal = Principal(user_id=7, company_id=1, department="Dispatch")

    await events_router._send_ip_alert(principal, "1.2.3.4")
    await events_router._send_ip_alert(principal, "1.2.3.4")

    assert "user: jane (Jane Doe) - id 7 (company 1)" in sent[0]
    assert "ip: 1.2.3.4" in sent[0]
    assert len(calls) == 1  # second alert served from the cache


async def test_alert_still_sends_when_lookup_fails(monkeypatch) -> None:
    sent = _capture(monkeypatch)

    async def broken(ids):
        raise RuntimeError("tms down")

    monkeypatch.setattr(boxtruck, "get_dispatchers_bulk", broken)

    await events_router._send_ip_alert(Principal(user_id=9, company_id=1, department=None), "5.6.7.8")

    assert "user: id 9 (company 1)" in sent[0]
