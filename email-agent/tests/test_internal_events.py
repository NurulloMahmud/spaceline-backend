"""Other services put events on the dispatcher stream through the internal route."""
from services import events

SECRET = {"X-Internal-Secret": "test-internal-secret"}


def test_hiring_event_reaches_the_hub(client, monkeypatch) -> None:
    seen: list[events.Event] = []
    monkeypatch.setattr(events.hub, "publish", seen.append)
    res = client.post(
        "/internal/v1/events",
        json={"type": "hiring_changed", "company_id": 1, "payload": {"driver_ids": [7]}},
        headers=SECRET,
    )
    assert res.status_code == 202
    assert [(e.type, e.company_id, e.payload) for e in seen] == [("hiring_changed", 1, {"driver_ids": [7]})]


def test_publish_needs_the_internal_secret(client) -> None:
    res = client.post("/internal/v1/events", json={"type": "hiring_changed", "company_id": 1})
    assert res.status_code == 401


def test_agent_event_types_cannot_be_forged(client) -> None:
    res = client.post("/internal/v1/events", json={"type": "load_booked", "company_id": 1}, headers=SECRET)
    assert res.status_code == 400
