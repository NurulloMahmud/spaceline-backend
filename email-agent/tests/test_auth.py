from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import HTTPException

from services import auth


def make_token(**overrides) -> str:
    """
    Mirrors Django's CustomTokenObtainPairSerializer exactly: `company` is the
    company NAME and `company_id` is the id. Getting this wrong in a fixture
    once hid a 500 on every authenticated request in production.
    """
    claims = {
        "token_type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "user_id": 7,
        "department": "Dispatch",
        "company": "Shipluxe LLC",
        "company_id": 1,
    }
    claims.update(overrides)
    return jwt.encode(claims, "test-signing-key", algorithm="HS256")


def test_accepts_a_real_django_access_token():
    """`company` is the name, `company_id` is the id — the real token shape."""
    principal = auth.current_user(f"Bearer {make_token()}")
    assert principal.user_id == 7
    assert principal.company_id == 1
    assert principal.company_name == "Shipluxe LLC"
    assert principal.department == "Dispatch"


def test_a_company_name_is_never_mistaken_for_an_id():
    """Reading `company` as the id used to raise ValueError -> HTTP 500."""
    token = make_token(company="J & J Wasatch Logistics", company_id=4)
    principal = auth.current_user(f"Bearer {token}")
    assert principal.company_id == 4


def test_missing_company_id_does_not_crash():
    """A user with no company must be refused (403 later), never a 500."""
    token = make_token(company="Shipluxe LLC")
    del_claims = jwt.decode(token, "test-signing-key", algorithms=["HS256"])
    del_claims.pop("company_id")
    token = jwt.encode(del_claims, "test-signing-key", algorithm="HS256")

    principal = auth.current_user(f"Bearer {token}")
    assert principal.company_id is None
    with pytest.raises(HTTPException) as e:
        auth.scoped_company(principal)
    assert e.value.status_code == 403


def test_null_company_id_is_handled():
    """Django emits company_id: null for a user with no company."""
    principal = auth.current_user(f"Bearer {make_token(company=None, company_id=None)}")
    assert principal.company_id is None


def test_reads_nested_company_and_department_objects():
    token = make_token(
        company={"id": 4, "name": "ShipLuxe"}, company_id=None, department={"name": "Management"}
    )
    principal = auth.current_user(f"Bearer {token}")
    assert principal.company_id == 4
    assert principal.is_management is True


def test_rejects_a_refresh_token():
    with pytest.raises(HTTPException) as e:
        auth.current_user(f"Bearer {make_token(token_type='refresh')}")
    assert e.value.status_code == 401
    assert "refresh" in e.value.detail.lower()


def test_rejects_a_token_signed_with_another_key():
    forged = jwt.encode(
        {
            "token_type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "user_id": 7,
            "company": 1,
        },
        "not-the-signing-key",
        algorithm="HS256",
    )
    with pytest.raises(HTTPException) as e:
        auth.current_user(f"Bearer {forged}")
    assert e.value.status_code == 401


def test_rejects_an_expired_token():
    expired = make_token(exp=datetime.now(timezone.utc) - timedelta(minutes=1))
    with pytest.raises(HTTPException) as e:
        auth.current_user(f"Bearer {expired}")
    assert "expired" in e.value.detail.lower()


def test_rejects_a_malformed_header():
    for header in ("", "token abc", "Bearer"):
        with pytest.raises(HTTPException):
            auth.current_user(header)


def test_a_user_without_a_company_cannot_reach_company_data():
    principal = auth.Principal(user_id=7, company_id=None, department="Dispatch")
    with pytest.raises(HTTPException) as e:
        auth.scoped_company(principal)
    assert e.value.status_code == 403


def test_internal_secret_must_match():
    assert auth.require_internal("test-internal-secret") is True
    for bad in ("", "wrong"):
        with pytest.raises(HTTPException):
            auth.require_internal(bad)
