"""Developer console endpoints. Developer department only, not management."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database.connection import session_dependency
from services import bid_health
from services.auth import Principal, current_user, scoped_company
from services.boxtruck import boxtruck

router = APIRouter(prefix="/api/v1/dev", tags=["dev"])


def require_developer(principal: Principal = Depends(current_user)) -> Principal:
    if not principal.is_developer:
        raise HTTPException(403, "developers only")
    return principal


@router.get("/bid-emails")
async def bid_emails(
    hours: int = Query(default=24, description="1, 24 or 168"),
    _: Principal = Depends(require_developer),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    if hours not in bid_health.WINDOWS:
        raise HTTPException(422, "hours must be 1, 24 or 168")
    data = bid_health.compute(session, company_id, hours)
    ids = [d["user_id"] for d in data["by_dispatcher"] if d["user_id"] is not None]
    people = await boxtruck.get_dispatchers_bulk(ids)
    for d in data["by_dispatcher"]:
        person = people.get(d["user_id"]) or {}
        full = f"{person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
        d["name"] = full or person.get("username")
    return data
