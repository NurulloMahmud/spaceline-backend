"""The notifications stream reports failures, not connections, to Telegram."""
import asyncio

from services import alerts


async def test_stream_failure_alert_is_throttled(monkeypatch) -> None:
    sent: list[str] = []

    async def fake_notify(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(alerts, "notify", fake_notify)
    monkeypatch.setattr(alerts, "_standing", {})

    alerts.stream_failed("RuntimeError: hub down")
    alerts.stream_failed("RuntimeError: hub down")
    await asyncio.sleep(0)

    assert sent == ["ALERT: notifications stream failed: RuntimeError: hub down"]
