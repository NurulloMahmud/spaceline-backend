"""The message fetch, which is where the Message-Id comes from."""
import httpx
import pytest

from services.nylas_client import NylasError, nylas


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def stub_get(monkeypatch, responses):
    """Answer each GET in turn, recording the params it was called with."""
    calls = []

    async def get(self, url, **kwargs):
        calls.append({"url": url, "params": kwargs.get("params")})
        return responses[len(calls) - 1]

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    return calls


async def test_get_message_asks_for_the_threading_headers(monkeypatch):
    message = {
        "id": "msg-1",
        "headers": [{"name": "Message-Id", "value": "<abc@mail.gmail.com>"}],
    }
    calls = stub_get(monkeypatch, [FakeResponse(200, {"data": message})])

    assert await nylas.get_message("grant-1", "msg-1") == message
    assert calls[0]["params"] == {"fields": "include_basic_headers"}


async def test_a_provider_that_refuses_the_parameter_still_gets_its_message(
    monkeypatch,
):
    """
    Without this the whole pipeline would stop for that mailbox: no message
    stored, no draft, no rate confirmation checked.
    """
    calls = stub_get(monkeypatch, [
        FakeResponse(400, text="unsupported field"),
        FakeResponse(200, {"data": {"id": "msg-1"}}),
    ])

    assert await nylas.get_message("grant-1", "msg-1") == {"id": "msg-1"}
    assert [c["params"] for c in calls] == [{"fields": "include_basic_headers"}, None]


async def test_a_failed_fetch_is_still_an_error(monkeypatch):
    stub_get(monkeypatch, [FakeResponse(404, text="not found")])

    with pytest.raises(NylasError):
        await nylas.get_message("grant-1", "msg-1")
