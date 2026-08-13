"""
Nylas lands the browser on this callback as a top-level navigation — nobody
reads the response body, the user is just looking at whatever tab it opened.
Every outcome must redirect back into the frontend; returning JSON or a raw
HTTP error strands the user on a blank API response instead.
"""
from urllib.parse import parse_qs, urlparse

from database import models
from routers.accounts import _sign
from services.nylas_client import NylasError, nylas


def _redirect_query(resp):
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    return urlparse(location).path, {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def test_successful_connect_redirects_to_settings(client, monkeypatch, session):
    async def fake_exchange(code):
        return {"grant_id": "grant-123", "email": "dispatch@shipluxellc.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1)},
        follow_redirects=False,
    )

    path, params = _redirect_query(resp)
    assert path == "/settings"
    assert params["mailbox"] == "connected"

    stored = session.query(models.EmailAccount).filter_by(company_id=1).one()
    assert stored.email_address == "dispatch@shipluxellc.com"
    assert stored.status == "active"


def test_reconnecting_updates_the_existing_row_not_a_duplicate(
    client, monkeypatch, session, account
):
    """`account` fixture already has company_id=1 connected — this is a reconnect."""
    async def fake_exchange(code):
        return {"grant_id": "grant-456", "email": "new-dispatch@shipluxellc.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1)},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["mailbox"] == "connected"
    assert session.query(models.EmailAccount).filter_by(company_id=1).count() == 1
    assert (
        session.query(models.EmailAccount).filter_by(company_id=1).one().email_address
        == "new-dispatch@shipluxellc.com"
    )


def test_user_declining_at_nylas_redirects_with_a_reason(client):
    resp = client.get(
        "/api/v1/accounts/callback",
        params={"error": "access_denied", "state": _sign(1)},
        follow_redirects=False,
    )
    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "declined"


def test_missing_code_or_state_redirects_instead_of_500(client):
    resp = client.get("/api/v1/accounts/callback", follow_redirects=False)
    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "missing_params"


def test_a_tampered_state_redirects_rather_than_raising(client):
    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": "1.0000000000000000000000000000000"},
        follow_redirects=False,
    )
    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "invalid_state"


def test_a_nylas_exchange_failure_redirects_rather_than_502ing(client, monkeypatch):
    async def failing_exchange(code):
        raise NylasError("token endpoint returned 400")

    monkeypatch.setattr(nylas, "exchange_code", failing_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "bad-code", "state": _sign(1)},
        follow_redirects=False,
    )
    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "exchange_failed"


def test_a_missing_grant_id_redirects_rather_than_502ing(client, monkeypatch, session):
    async def fake_exchange(code):
        return {"email": "dispatch@shipluxellc.com"}  # no grant_id

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1)},
        follow_redirects=False,
    )
    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "no_grant"
    assert session.query(models.EmailAccount).filter_by(company_id=1).count() == 0
