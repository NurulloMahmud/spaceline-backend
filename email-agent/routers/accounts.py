"""
Where a mailbox stands, for the pages that only need to know whether one is
connected. Connecting and changing mailboxes is an administrator's job under
Admin > Mailboxes (routers/mailboxes.py); nothing here writes.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import models
from database.connection import session_dependency
from services.auth import Principal, current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


@router.get("/me")
def get_my_account(
    principal: Principal = Depends(current_user),
    session: Session = Depends(session_dependency),
):
    """
    The caller's own mailbox. Bids leave from it and from nowhere else, so
    the load board shows "not connected" instead of a Send button while it is
    missing. `enabled` is kept for the frontend and is always true.
    """
    enabled = True
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
