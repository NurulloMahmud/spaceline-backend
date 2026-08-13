"""
Only management can connect (or reconnect) a company's dispatch mailbox — it
sends every bid and receives every broker reply for the whole company, so a
regular dispatcher must not be able to repoint it.
"""
import pytest

from config.settings import config
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
