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


def test_a_grant_for_a_different_mailbox_is_refused(client, monkeypatch, session):
    """
    login_hint only preselects an account — whoever is at the consent screen
    can still sign into another one. Storing that grant would point this
    company's dispatch mail at someone else's inbox.
    """
    revoked = []

    async def fake_exchange(code):
        return {"grant_id": "grant-999", "email": "someone.else@gmail.com"}

    async def fake_revoke(grant_id):
        revoked.append(grant_id)

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)
    monkeypatch.setattr(nylas, "revoke_grant", fake_revoke)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1, "dispatch@shipluxellc.com")},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["mailbox"] == "error"
    assert params["reason"] == "wrong_mailbox"
    assert session.query(models.EmailAccount).filter_by(company_id=1).count() == 0
    # the access we just gained is handed back rather than left live
    assert revoked == ["grant-999"]


def test_a_refused_grant_does_not_disturb_the_existing_connection(
    client, monkeypatch, session, account
):
    """A failed repoint must leave the working mailbox exactly as it was."""
    async def fake_exchange(code):
        return {"grant_id": "grant-999", "email": "someone.else@gmail.com"}

    async def fake_revoke(grant_id):
        return None

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)
    monkeypatch.setattr(nylas, "revoke_grant", fake_revoke)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1, "dispatch@shipluxellc.com")},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["reason"] == "wrong_mailbox"

    session.expire_all()
    stored = session.query(models.EmailAccount).filter_by(company_id=1).one()
    assert stored.nylas_grant_id == "grant-1"
    assert stored.email_address == "dispatch@shipluxellc.com"


def test_the_expected_mailbox_matches_regardless_of_casing(client, monkeypatch, session):
    """The provider chooses how it capitalises the address it reports."""
    async def fake_exchange(code):
        return {"grant_id": "grant-123", "email": "Dispatch@ShipLuxeLLC.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1, "dispatch@shipluxellc.com")},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["mailbox"] == "connected"
    stored = session.query(models.EmailAccount).filter_by(company_id=1).one()
    assert stored.expected_email_address == "dispatch@shipluxellc.com"


def test_a_matching_grant_is_stored_with_what_was_expected(client, monkeypatch, session):
    async def fake_exchange(code):
        return {"grant_id": "grant-123", "email": "dispatch@shipluxellc.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1, "dispatch@shipluxellc.com")},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["mailbox"] == "connected"
    stored = session.query(models.EmailAccount).filter_by(company_id=1).one()
    assert stored.nylas_grant_id == "grant-123"
    assert stored.expected_email_address == "dispatch@shipluxellc.com"


def test_a_state_naming_a_mailbox_cannot_have_it_swapped_in_the_address_bar(client):
    """
    The expected mailbox is inside the signature, so editing it invalidates
    the state rather than relaxing the check it drives.
    """
    tampered = _sign(1, "dispatch@shipluxellc.com").replace(
        "dispatch@shipluxellc.com", "attacker@gmail.com"
    )
    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": tampered},
        follow_redirects=False,
    )
    _, params = _redirect_query(resp)
    assert params["reason"] == "invalid_state"


def test_a_state_signed_without_a_mailbox_still_connects(client, monkeypatch, session):
    """
    A connect already in flight when this deploys carries the old two-part
    state. It has no address to check, and must complete rather than error.
    """
    async def fake_exchange(code):
        return {"grant_id": "grant-123", "email": "whatever@shipluxellc.com"}

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1)},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["mailbox"] == "connected"
    stored = session.query(models.EmailAccount).filter_by(company_id=1).one()
    assert stored.expected_email_address is None


def test_a_revoke_failure_still_refuses_the_grant(client, monkeypatch, session):
    """Handing the grant back is best-effort; refusing it is not."""
    async def fake_exchange(code):
        return {"grant_id": "grant-999", "email": "someone.else@gmail.com"}

    async def failing_revoke(grant_id):
        raise NylasError("grant revoke failed (500)")

    monkeypatch.setattr(nylas, "exchange_code", fake_exchange)
    monkeypatch.setattr(nylas, "revoke_grant", failing_revoke)

    resp = client.get(
        "/api/v1/accounts/callback",
        params={"code": "abc", "state": _sign(1, "dispatch@shipluxellc.com")},
        follow_redirects=False,
    )

    _, params = _redirect_query(resp)
    assert params["reason"] == "wrong_mailbox"
    assert session.query(models.EmailAccount).filter_by(company_id=1).count() == 0
