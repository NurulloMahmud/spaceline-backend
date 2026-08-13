"""
Connecting a company's shared dispatch mailbox through Nylas hosted auth.

The `state` parameter carries the company id so the callback knows which
company the returned grant belongs to; it is signed to keep it from being
forged into a grant on another company's behalf.
"""
import hashlib
import hmac
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from config.settings import config
from database import models
from database.connection import session_dependency
from routers.schemas import ConnectAccountRequest
from services.auth import Principal, current_user, scoped_company
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _sign(company_id: int) -> str:
    mac = hmac.new(
        config.INTERNAL_SECRET_KEY.encode(), str(company_id).encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{company_id}.{mac}"


def _verify(state: str) -> int:
    try:
        raw_company, mac = state.split(".", 1)
        company_id = int(raw_company)
    except (ValueError, AttributeError):
        raise HTTPException(400, "malformed state")
    if not hmac.compare_digest(_sign(company_id), state):
        raise HTTPException(400, "state signature mismatch")
    return company_id


def _settings_redirect(status: str, **params) -> RedirectResponse:
    """
    Every outcome of the OAuth callback ends here. Nylas leaves the browser
    on our callback URL as a top-level navigation — nobody reads its response
    body — so returning JSON stranded the user on a blank API response
    instead of putting them back on the mailbox settings page.

    Falls back to a same-origin relative path when no frontend URL is
    configured, which is correct whenever the frontend and this service
    share a host (the deployed case: both behind boxmanage.smartfleetllc.com).
    """
    target = config.FRONTEND_MAILBOX_SETTINGS_URL or "/settings"
    query = urlencode({"mailbox": status, **params})
    return RedirectResponse(url=f"{target}?{query}", status_code=302)


@router.get("")
def get_account(
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == company_id)
        .first()
    )
    if not account:
        return {"connected": False}
    return {
        "connected": True,
        "email_address": account.email_address,
        "status": account.status,
        "connected_at": account.created_at,
    }


@router.post("/connect")
def connect(
    body: ConnectAccountRequest,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
):
    """
    Returns the URL the browser should open to authorise the mailbox.

    Management only. The mailbox sends every bid and receives every broker
    reply for the whole company, so a regular dispatcher must not be able to
    repoint it — accidentally or otherwise — to a different inbox.
    """
    if not principal.is_management:
        raise HTTPException(403, "only management users can connect a mailbox")

    target = body.company_id if body.company_id else company_id

    if not config.NYLAS_CLIENT_ID or not config.NYLAS_CALLBACK_URI:
        raise HTTPException(500, "Nylas hosted auth is not configured")

    return {"auth_url": nylas.hosted_auth_url(state=_sign(target))}


@router.get("/callback")
async def callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    session: Session = Depends(session_dependency),
):
    """
    Nylas sends the browser here after the mailbox owner authorises or
    declines. This is a top-level navigation, not a call a frontend inspects
    the body of, so every branch redirects back into the app rather than
    returning JSON or raising an HTTP error the user would see raw.
    """
    if error:
        logger.warning(f"nylas hosted auth returned an error: {error}")
        return _settings_redirect("error", reason="declined")

    if not code or not state:
        return _settings_redirect("error", reason="missing_params")

    try:
        company_id = _verify(state)
    except HTTPException as e:
        logger.warning(f"mailbox callback rejected: {e.detail}")
        return _settings_redirect("error", reason="invalid_state")

    try:
        grant = await nylas.exchange_code(code)
    except NylasError as e:
        logger.error(f"nylas code exchange failed for company {company_id}: {e}")
        return _settings_redirect("error", reason="exchange_failed")

    grant_id = grant.get("grant_id")
    email_address = grant.get("email") or grant.get("email_address")
    if not grant_id:
        logger.error(f"nylas returned no grant_id for company {company_id}")
        return _settings_redirect("error", reason="no_grant")

    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == company_id)
        .first()
    )
    if account:
        account.nylas_grant_id = grant_id
        account.email_address = email_address or account.email_address
        account.status = "active"
    else:
        session.add(
            models.EmailAccount(
                company_id=company_id,
                nylas_grant_id=grant_id,
                email_address=email_address or "",
                status="active",
            )
        )
    session.flush()

    return _settings_redirect("connected")
