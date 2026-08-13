"""
Django SimpleJWT validation, done locally against the shared signing key —
the same approach atrek takes, so there is no callback to the TMS on the hot
path. JWT_SECRET must equal Django's SIMPLE_JWT signing key.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Query, Request, status

from config.settings import config

logger = logging.getLogger(__name__)


@dataclass
class Principal:
    user_id: int
    company_id: Optional[int]
    department: Optional[str]
    company_name: Optional[str] = None

    @property
    def is_management(self) -> bool:
        return (self.department or "").lower() == "management"


def _company_id_from(claims: dict) -> Optional[int]:
    """
    Django's CustomTokenObtainPairSerializer sets `company` to the company
    *name* and `company_id` to the id, so the id claim is the one to read —
    reading `company` yields 'Shipluxe LLC' and blows up on int().

    The fallbacks cover only shapes that are unambiguously an id; a string
    company is a name and is never treated as one.
    """
    candidate = claims.get("company_id")

    if candidate is None:
        company = claims.get("company")
        if isinstance(company, dict):
            candidate = company.get("id")
        elif isinstance(company, bool):
            candidate = None
        elif isinstance(company, int):
            candidate = company

    if candidate is None:
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        logger.warning(f"unusable company id claim: {candidate!r}")
        return None


def _decode(token: str) -> Principal:
    try:
        claims = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has expired")
    except jwt.InvalidTokenError as e:
        logger.warning(f"jwt rejected: {e}")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    if claims.get("token_type") != "access":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "refresh tokens cannot be used to authenticate requests",
        )

    user_id = claims.get("user_id")
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token is missing user_id")

    department = claims.get("department")
    if isinstance(department, dict):
        department = department.get("name")

    company = claims.get("company")
    company_name = company if isinstance(company, str) else None
    if isinstance(company, dict):
        company_name = company.get("name")

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token has an invalid user_id")

    return Principal(
        user_id=user_id,
        company_id=_company_id_from(claims),
        department=department,
        company_name=company_name,
    )


def current_user(authorization: str = Header(default="")) -> Principal:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authorization header must be 'Bearer <token>'",
        )
    return _decode(parts[1])


def current_user_sse(
    request: Request,
    token: str = Query(default=""),
) -> Principal:
    """EventSource cannot set headers, so the SSE route also accepts ?token=."""
    header = request.headers.get("authorization", "")
    if header:
        return current_user(header)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    return _decode(token)


def scoped_company(principal: Principal = Depends(current_user)) -> int:
    """Every dispatcher-facing query is bounded by this."""
    if principal.company_id is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "your account is not linked to a company",
        )
    return principal.company_id


def require_internal(x_internal_secret: str = Header(default="")) -> bool:
    if not config.INTERNAL_SECRET_KEY or x_internal_secret != config.INTERNAL_SECRET_KEY:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal secret")
    return True
