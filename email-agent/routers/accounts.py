"""
Connecting a company's shared dispatch mailbox through Nylas hosted auth.

The `state` parameter carries the company id so the callback knows which
company the returned grant belongs to, and the mailbox we expect back when
management named one; it is signed to keep either from being forged into a
grant on another company's behalf.
"""
import hashlib
import hmac
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import config
from database import models
from database.connection import session_dependency
from routers.schemas import ConnectAccountRequest
from services.auth import Principal, current_user, scoped_company
from services.nylas_client import NylasError, nylas

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


def _sign(company_id: int, expected_email: str = "") -> str:
    """
    The expected mailbox travels inside the signature rather than as a
    separate parameter, so it cannot be edited in the address bar between
    the consent screen and the callback — which would defeat the check it
    exists to drive.
    """
    payload = f"{company_id}.{expected_email}" if expected_email else str(company_id)
    mac = hmac.new(
        config.INTERNAL_SECRET_KEY.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload}.{mac}"


def _verify(state: str) -> tuple[int, str]:
    """
    Returns the company id and the mailbox the grant must match, which is ""
    when the URL was issued without one. States signed before the expected
    mailbox existed carry no address and still verify, so a connect already
    in flight when this deploys completes instead of erroring.

    The mac is taken from the right: an email address contains dots, the
    company id and the mac do not.
    """
    try:
        payload, mac = state.rsplit(".", 1)
        company_id = int(payload.split(".", 1)[0])
    except (ValueError, AttributeError):
        raise HTTPException(400, "malformed state")
    expected_email = payload.split(".", 1)[1] if "." in payload else ""
    if not hmac.compare_digest(_sign(company_id, expected_email), state):
        raise HTTPException(400, "state signature mismatch")
    return company_id, expected_email


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

    # Lowercased once here so the hint, the signature and the comparison in
    # the callback all speak of the same address; providers are free to
    # return a different casing than the one that was typed.
    expected_email = (body.email_address or "").strip().lower()

    return {
        "auth_url": nylas.hosted_auth_url(
            state=_sign(target, expected_email), login_hint=expected_email
        )
    }


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
        company_id, expected_email = _verify(state)
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

    # `login_hint` only preselects an account; the person at the consent
    # screen can still sign into a different one. Storing that grant would
    # point a company's dispatch mail at the wrong mailbox — sending its bids
    # from it and reading a stranger's inbox — so the address that came back
    # decides, not the one we asked for.
    if expected_email and (email_address or "").strip().lower() != expected_email:
        logger.error(
            f"mailbox mismatch for company {company_id}: authorised "
            f"{email_address or '<none>'}, expected {expected_email} — grant rejected"
        )
        try:
            await nylas.revoke_grant(grant_id)
        except NylasError as e:
            # The grant is already refused; failing to hand it back is worth
            # knowing about but must not change what the user is told.
            logger.error(f"could not revoke rejected grant for company {company_id}: {e}")
        return _settings_redirect("error", reason="wrong_mailbox")

    # One mailbox backs exactly one company: `nylas_grant_id` is unique. A
    # mailbox another company already uses reached the write below and died on
    # that index, so the browser got a raw 500 in the middle of the consent
    # flow instead of being told what happened. The clash is caught here, and
    # the grant is deliberately *not* revoked — the other company is sending
    # on it right now.
    clash = (
        session.query(models.EmailAccount)
        .filter(
            models.EmailAccount.nylas_grant_id == grant_id,
            models.EmailAccount.company_id != company_id,
        )
        .first()
    )
    if clash:
        logger.error(
            f"mailbox {email_address or '<unknown>'} is already connected to "
            f"company {clash.company_id}; refusing to attach it to company "
            f"{company_id}"
        )
        return _settings_redirect("error", reason="mailbox_in_use")

    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == company_id)
        .first()
    )
    if account:
        account.nylas_grant_id = grant_id
        account.email_address = email_address or account.email_address
        account.status = "active"
        if expected_email:
            account.expected_email_address = expected_email
    else:
        session.add(
            models.EmailAccount(
                company_id=company_id,
                nylas_grant_id=grant_id,
                email_address=email_address or "",
                expected_email_address=expected_email or None,
                status="active",
            )
        )
    try:
        session.flush()
    except IntegrityError:
        # Lost a race with a simultaneous consent for the same mailbox.
        session.rollback()
        logger.error(
            f"mailbox {email_address or '<unknown>'} was claimed concurrently; "
            f"company {company_id} not connected"
        )
        return _settings_redirect("error", reason="mailbox_in_use")

    return _settings_redirect("connected")
