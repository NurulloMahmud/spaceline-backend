"""
Where a mailbox stands, for the pages that only need to know whether one is
connected. Connecting and changing mailboxes is an administrator's job under
Admin > Mailboxes (routers/mailboxes.py); nothing here writes.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from config.settings import config
from database import models
from database.connection import session_dependency
from services.auth import Principal, current_user, scoped_company
from services.negotiations import shared_mailbox

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


@router.get("")
def get_account(
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """The company's shared mailbox: the one dispatchers without a mailbox
    of their own send from."""
    account = shared_mailbox(session, company_id)
    if not account:
        return {"connected": False}
    return {
        "connected": True,
        "email_address": account.email_address,
        "status": account.status,
        "connected_at": account.created_at,
    }


@router.get("/me")
def get_my_account(
    principal: Principal = Depends(current_user),
    session: Session = Depends(session_dependency),
):
    """
    The caller's own mailbox. `enabled` says whether bids must leave from a
    mailbox of their own (the personal-mailbox rollout), so the load board
    can say that one is missing.
    """
    enabled = config.personal_mailbox_allowed(principal.user_id)
    account = (
        session.query(models.EmailAccount)
        .filter(models.EmailAccount.user_id == principal.user_id)
        .first()
    )
    if not account or account.status == "revoked":
        return {"enabled": enabled, "connected": False}
    return {
        "enabled": enabled,
        "connected": True,
        "email_address": account.email_address,
        "status": account.status,
        "connected_at": account.created_at,
    }
