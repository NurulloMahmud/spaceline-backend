"""
Only management can connect (or reconnect) a company's dispatch mailbox — it
sends every bid and receives every broker reply for the whole company, so a
regular dispatcher must not be able to repoint it.
"""
from urllib.parse import parse_qs, urlparse

import pytest

from config.settings import config
from routers.accounts import _sign
from tests.test_auth import make_token


@pytest.fixture
def nylas_configured(monkeypatch):
    monkeypatch.setattr(config, "NYLAS_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "NYLAS_CALLBACK_URI", "https://example.com/callback")


def test_a_dispatcher_cannot_connect_a_mailbox(client, nylas_configured):
    token = make_token(department="Dispatch")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 403
    assert "management" in resp.json()["detail"].lower()


def test_a_billing_user_cannot_connect_a_mailbox(client, nylas_configured):
    token = make_token(department="Billing")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 403


def test_a_management_user_can_connect_their_own_company(client, nylas_configured):
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 200
    assert "auth_url" in resp.json()
    assert resp.json()["auth_url"].startswith("https://")


def test_a_dispatcher_cannot_connect_another_companys_mailbox_either(client, nylas_configured):
    """The permission check applies before the cross-company target is even read."""
    token = make_token(department="Dispatch", company="Smart Fleet LLC", company_id=2)
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"company_id": 4},
    )
    assert resp.status_code == 403


def test_a_management_user_can_still_connect_a_different_companys_mailbox(
    client, nylas_configured
):
    """Management retains the existing cross-company ability — only the department gate is new."""
    token = make_token(department="Management", company="Smart Fleet LLC", company_id=2)
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"company_id": 4},
    )
    assert resp.status_code == 200
    # the signed state should carry the target company (4), not the caller's own (2)
    assert "state=4." in resp.json()["auth_url"]


def test_reading_connection_status_still_needs_no_special_role(client, nylas_configured):
    """Only the write action is management-gated; checking status is unaffected."""
    token = make_token(department="Dispatch")
    resp = client.get(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_the_auth_url_carries_the_offline_access_type(client, nylas_configured):
    """Without it the grant has no refresh token and dies with the first access token."""
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert "access_type=offline" in resp.json()["auth_url"]


def test_naming_a_mailbox_preselects_it_on_the_consent_screen(client, nylas_configured):
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"email_address": "dispatch@shipluxellc.com"},
    )
    assert resp.status_code == 200
    query = parse_qs(urlparse(resp.json()["auth_url"]).query)
    assert query["login_hint"] == ["dispatch@shipluxellc.com"]
    # and it rides along in the signed state, so the callback can insist on it
    assert query["state"] == [_sign(1, "dispatch@shipluxellc.com")]


def test_a_mailbox_is_optional(client, nylas_configured):
    """Omitting it accepts whichever mailbox is authorised, as before."""
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert "login_hint" not in parse_qs(urlparse(resp.json()["auth_url"]).query)


def test_the_named_mailbox_is_normalised_before_it_is_signed(client, nylas_configured):
    """
    The provider decides the casing it reports back. Folding both sides once,
    here, keeps a capitalised entry from failing its own verification later.
    """
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"email_address": "Dispatch@ShipLuxeLLC.com"},
    )
    query = parse_qs(urlparse(resp.json()["auth_url"]).query)
    assert query["login_hint"] == ["dispatch@shipluxellc.com"]
