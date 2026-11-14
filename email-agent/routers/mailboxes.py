"""
Mailboxes connected with an app password, entered by an admin for each
dispatcher.

The dispatcher hands over nothing but the app password their provider
issued; the admin enters it here, the service signs in both ways to prove
it, and from then on that dispatcher's bids leave over SMTP from their own
address and their inbox is read over IMAP (services.shared_inbox).

Open to Management and Developer accounts, plus any BTM user id listed in
MAILBOX_ADMIN_USERS: every row here is a credential to someone else's
mailbox. The posted-loads listener mailbox, which feeds the load board, is
developers' alone.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from config.settings import config
from database import models
from database.connection import session_dependency
from routers.schemas import MailboxCredentialsIn
from services import imap_smtp, secrets
from services.auth import Principal, current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mailboxes", tags=["mailboxes"])


def mailbox_admin(principal: Principal = Depends(current_user)) -> Principal:
    if not (principal.is_management or config.mailbox_admin(principal.user_id)):
        raise HTTPException(403, "this page is open to management and developers only")
    if principal.company_id is None:
        raise HTTPException(403, "your account is not linked to a company")
    return principal


def developer_only(principal: Principal = Depends(mailbox_admin)) -> Principal:
    if not principal.is_developer:
        raise HTTPException(403, "only a developer can change the posted-loads listener mailbox")
    return principal


def listener_view(row: Optional[models.ListenerMailbox]) -> Optional[dict]:
    if row is None:
        return None
    return {
        "email_address": row.email_address,
        "imap_host": row.imap_host,
        "imap_port": row.imap_port,
        "folder": row.folder,
        "updated_at": row.updated_at,
        "updated_by_user_id": row.updated_by_user_id,
    }


def _listener_row(session: Session, company_id: int) -> Optional[models.ListenerMailbox]:
    return (
        session.query(models.ListenerMailbox)
        .filter(models.ListenerMailbox.company_id == company_id)
        .order_by(models.ListenerMailbox.updated_at.desc())
        .first()
    )


def view(account: models.EmailAccount) -> dict:
    return {
        "user_id": account.user_id,
        "email_address": account.email_address,
        "transport": account.transport,
        "status": account.status,
        "imap_host": account.imap_host,
        "imap_port": account.imap_port,
        "smtp_host": account.smtp_host,
        "smtp_port": account.smtp_port,
        "last_checked_at": account.last_checked_at,
        "last_error": account.last_error,
        "connected_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _credentials(body: MailboxCredentialsIn, password: str) -> imap_smtp.Credentials:
    if not body.imap_only and not (body.smtp_host or "").strip():
        raise HTTPException(422, "an SMTP host is required for a mailbox that sends")
    return imap_smtp.Credentials(
        address=str(body.email_address).strip().lower(),
        password=password,
        imap_host=body.imap_host.strip(),
        imap_port=int(body.imap_port),
        smtp_host=(body.smtp_host or "").strip(),
        smtp_port=int(body.smtp_port),
        folder=(body.folder or "INBOX").strip() or "INBOX",
    )


def _stored_credentials(account: models.EmailAccount) -> imap_smtp.Credentials:
    if not account.uses_imap or not account.secret:
        raise HTTPException(409, f"{account.email_address} is not connected with an app password")
    try:
        password = secrets.decrypt(account.secret)
    except secrets.SecretError as e:
        raise HTTPException(500, str(e))
    return imap_smtp.Credentials(
        address=account.email_address,
        password=password,
        imap_host=account.imap_host or "",
        imap_port=int(account.imap_port or 993),
        smtp_host=account.smtp_host or "",
        smtp_port=int(account.smtp_port or 465),
    )


def _user_account(session: Session, company_id: int, user_id: int) -> Optional[models.EmailAccount]:
    return (
        session.query(models.EmailAccount)
        .filter(
            models.EmailAccount.user_id == user_id,
            models.EmailAccount.company_id == company_id,
        )
        .first()
    )


def _record_check(account: models.EmailAccount, result: imap_smtp.CheckResult) -> None:
    """What a test from the Mailboxes page leaves on the row. Only a refused
    password marks the mailbox failing, which stops its bids; a test that
    could not reach the provider (a timeout, a dropped connection) is kept as
    the last error and the mailbox stays as it was, as in the inbox reader."""
    account.last_checked_at = result.checked_at
    if result.ok:
        account.last_error = None
        if account.status == "failing":
            account.status = "active"
    else:
        account.last_error = "; ".join(
            part for part in (
                f"IMAP: {result.imap_error}" if result.imap_error else "",
                f"SMTP: {result.smtp_error}" if result.smtp_error else "",
            ) if part
        )
        if result.refused and account.status == "active":
            account.status = "failing"


@router.get("")
def list_mailboxes(
    principal: Principal = Depends(mailbox_admin),
    session: Session = Depends(session_dependency),
):
    """Every mailbox of the company: the shared one as the server has it,
    and one row per dispatcher."""
    rows = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.company_id == principal.company_id)
        .order_by(models.EmailAccount.created_at)
        .all()
    )
    shared_row = next((r for r in rows if r.user_id is None), None)
    cursor = (
        session.get(models.SharedInboxCursor, config.SHARED_INBOX_ADDRESS)
        if config.SHARED_INBOX_ADDRESS else None
    )
    return {
        "shared": {
            "address": config.SHARED_INBOX_ADDRESS or None,
            "configured": config.shared_inbox_enabled(),
            "imap_host": config.SHARED_INBOX_IMAP_HOST,
            "folder": config.SHARED_INBOX_FOLDER,
            "last_read_at": cursor.updated_at if cursor else None,
            "account": view(shared_row) if shared_row else None,
        },
        "accounts": [view(r) for r in rows if r.user_id is not None],
        "listener": listener_view(_listener_row(session, principal.company_id)),
        "can_manage_listener": principal.is_developer,
        "secret_key_set": bool(config.MAILBOX_SECRET_KEY),
    }


@router.put("/listener")
async def set_listener(
    body: MailboxCredentialsIn,
    principal: Principal = Depends(developer_only),
    session: Session = Depends(session_dependency),
):
    """
    The mailbox the Sylectus posted-loads listener reads: address, app
    password and the folder the alerts are filed in. Proven over IMAP before
    it is stored; the listener picks the change up within a minute without
    a restart. Leaving the password out keeps the stored one.
    """
    if not config.MAILBOX_SECRET_KEY:
        raise HTTPException(500, "MAILBOX_SECRET_KEY is not set on the server, so app passwords cannot be stored")
    existing = _listener_row(session, principal.company_id)
    password = (body.password or "").strip()
    if not password:
        if existing is None:
            raise HTTPException(422, "an app password is required")
        try:
            password = secrets.decrypt(existing.secret)
        except secrets.SecretError as e:
            raise HTTPException(500, str(e))
    body.imap_only = True
    creds = _credentials(body, password)
    result = await asyncio.to_thread(imap_smtp.check, creds, False)
    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "The mailbox did not sign in; nothing was saved.",
                "code": "check_failed",
                "result": result.as_dict(),
            },
        )
    try:
        stored_secret = secrets.encrypt(password)
    except secrets.SecretError as e:
        raise HTTPException(500, str(e))
    if existing is None:
        existing = models.ListenerMailbox(company_id=principal.company_id, secret=stored_secret)
        session.add(existing)
    existing.email_address = creds.address
    existing.imap_host = creds.imap_host
    existing.imap_port = creds.imap_port
    existing.folder = creds.folder
    existing.secret = stored_secret
    existing.updated_by_user_id = principal.user_id
    existing.updated_at = datetime.now(timezone.utc)
    session.flush()
    logger.info(
        f"developer {principal.user_id} pointed the posted-loads listener at "
        f"{creds.address} ({creds.folder})"
    )
    return {"listener": listener_view(existing), "result": result.as_dict()}


@router.post("/test")
async def test_credentials(
    body: MailboxCredentialsIn,
    principal: Principal = Depends(mailbox_admin),
):
    """Sign in both ways with what was typed, without storing anything."""
    if not body.password:
        raise HTTPException(422, "an app password is required to test")
    result = await asyncio.to_thread(imap_smtp.check, _credentials(body, body.password), not body.imap_only)
    return result.as_dict()


@router.put("/users/{user_id}")
async def connect_user(
    user_id: int,
    body: MailboxCredentialsIn,
    principal: Principal = Depends(mailbox_admin),
    session: Session = Depends(session_dependency),
):
    """
    Store a dispatcher's mailbox after proving it signs in. An existing
    mailbox of theirs is replaced. Leaving the password out keeps the one
    already stored (editing hosts or ports).
    """
    if not config.MAILBOX_SECRET_KEY:
        raise HTTPException(500, "MAILBOX_SECRET_KEY is not set on the server, so app passwords cannot be stored")

    existing = _user_account(session, principal.company_id, user_id)
    password = (body.password or "").strip()
    if not password:
        if existing and existing.uses_imap and existing.secret:
            try:
                password = secrets.decrypt(existing.secret)
            except secrets.SecretError as e:
                raise HTTPException(500, str(e))
        else:
            raise HTTPException(422, "an app password is required")

    creds = _credentials(body, password)
    result = await asyncio.to_thread(imap_smtp.check, creds)
    if not result.ok:
        return JSONResponse(
            status_code=422,
            content={
                "detail": "The mailbox did not sign in; nothing was saved.",
                "code": "check_failed",
                "result": result.as_dict(),
            },
        )

    try:
        stored_secret = secrets.encrypt(password)
    except secrets.SecretError as e:
        raise HTTPException(500, str(e))

    if existing is None:
        existing = models.EmailAccount(
            company_id=principal.company_id,
            user_id=user_id,
            nylas_grant_id=f"imap:{uuid.uuid4()}",
            email_address=creds.address,
        )
        session.add(existing)
    elif not existing.nylas_grant_id.startswith("imap:"):
        existing.nylas_grant_id = f"imap:{uuid.uuid4()}"

    existing.transport = models.TRANSPORT_IMAP
    existing.email_address = creds.address
    existing.expected_email_address = creds.address
    existing.imap_host = creds.imap_host
    existing.imap_port = creds.imap_port
    existing.smtp_host = creds.smtp_host
    existing.smtp_port = creds.smtp_port
    existing.secret = stored_secret
    existing.status = "active"
    existing.last_checked_at = result.checked_at
    existing.last_error = None
    existing.connected_by_user_id = principal.user_id
    session.flush()
    logger.info(f"user {principal.user_id} connected {creds.address} for user {user_id} with an app password")
    return {"account": view(existing), "result": result.as_dict()}


@router.post("/users/{user_id}/test")
async def test_user(
    user_id: int,
    principal: Principal = Depends(mailbox_admin),
    session: Session = Depends(session_dependency),
):
    account = _user_account(session, principal.company_id, user_id)
    if account is None:
        raise HTTPException(404, "this user has no mailbox")
    creds = _stored_credentials(account)
    result = await asyncio.to_thread(imap_smtp.check, creds)
    _record_check(account, result)
    session.flush()
    return {"account": view(account), "result": result.as_dict()}


@router.delete("/users/{user_id}")
async def disconnect_user(
    user_id: int,
    principal: Principal = Depends(mailbox_admin),
    session: Session = Depends(session_dependency),
):
    """Forget the password and stop reading. The row stays, revoked, so the
    negotiations it sent keep their mailbox and the dispatcher is told to
    connect again rather than silently falling back."""
    account = _user_account(session, principal.company_id, user_id)
    if account is None or account.status == "revoked":
        return {"connected": False}
    account.secret = None
    account.status = "revoked"
    account.last_error = None
    account.updated_at = datetime.now(timezone.utc)
    session.flush()
    logger.info(f"user {principal.user_id} disconnected {account.email_address} for user {user_id}")
    return {"connected": False}
