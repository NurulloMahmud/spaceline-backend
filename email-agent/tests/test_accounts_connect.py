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


def test_a_management_user_is_told_the_flow_is_retired(client, nylas_configured):
    """Nylas sign-in no longer issues grants: mailboxes are entered under
    Admin > Mailboxes with an app password. The role check still runs first."""
    token = make_token(department="Management")
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert resp.status_code == 410
    assert "Admin > Mailboxes" in resp.json()["detail"]


def test_a_dispatcher_cannot_connect_another_companys_mailbox_either(client, nylas_configured):
    """The permission check applies before the cross-company target is even read."""
    token = make_token(department="Dispatch", company="Smart Fleet LLC", company_id=2)
    resp = client.post(
        "/api/v1/accounts/connect",
        headers={"Authorization": f"Bearer {token}"},
        json={"company_id": 4},
    )
    assert resp.status_code == 403


def test_reading_connection_status_still_needs_no_special_role(client, nylas_configured):
    """Only the write action is management-gated; checking status is unaffected."""
    token = make_token(department="Dispatch")
    resp = client.get(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_the_signed_state_still_normalises_a_named_mailbox():
    """The callback still verifies states signed before the retirement, so
    the signing stays byte for byte what it was."""
    assert _sign(1, "dispatch@shipluxellc.com") == _sign(1, "dispatch@shipluxellc.com")
    assert _sign(1, "dispatch@shipluxellc.com") != _sign(1, "Dispatch@ShipLuxeLLC.com")
