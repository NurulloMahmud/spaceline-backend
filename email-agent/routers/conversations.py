"""
Conversations: the negotiations as email threads, for the Conversations page.

Anyone in the company can read every conversation. What is new is tracked per
user. Only the dispatcher who sent the bid can write in it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import models
from database.connection import session_dependency
from routers import schemas
from services import conversations as service
from services.auth import Principal, current_user, scoped_company
from services.boxtruck import boxtruck
from services.negotiations import NegotiationError

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _http(e: NegotiationError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail={"error": e.message, "code": e.code})


@router.get("")
async def list_conversations(
    who: str = Query(default="all", description="all, me, or a dispatcher's user id"),
    unread: bool = Query(default=False, description="only conversations with broker messages new to me"),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=25, ge=1, le=100),
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """Latest message first, with what is new to the caller and whether they can write."""
    query = session.query(models.Negotiation).filter(models.Negotiation.company_id == company_id)
    if who == "me":
        query = query.filter(models.Negotiation.dispatcher_user_id == principal.user_id)
    elif who not in ("", "all"):
        try:
            query = query.filter(models.Negotiation.dispatcher_user_id == int(who))
        except ValueError:
            raise HTTPException(422, detail={"error": "who must be all, me or a user id", "code": "invalid_who"})
    if unread:
        query = query.filter(service.has_unread(principal.user_id))

    total = query.count()
    rows = (
        query.order_by(service.last_activity().desc(), models.Negotiation.id)
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    ids = [r.id for r in rows]
    counts = service.unread_counts(session, principal.user_id, ids)
    latest = service.last_messages(session, ids)
    drivers = await boxtruck.get_drivers_bulk(list({r.driver_id for r in rows if r.driver_id is not None}))
    dispatchers = await boxtruck.get_dispatchers_bulk(
        list({r.dispatcher_user_id for r in rows if r.dispatcher_user_id is not None})
    )

    items = []
    for r in rows:
        item = schemas.negotiation_to_out(
            r, driver=drivers.get(r.driver_id), dispatcher=dispatchers.get(r.dispatcher_user_id)
        )
        last = latest.get(r.id)
        item["unread"] = counts.get(r.id, 0)
        item["last_message"] = service.snippet(last)
        item["last_activity_at"] = last.created_at if last else r.created_at
        item["can_write"] = r.dispatcher_user_id is None or r.dispatcher_user_id == principal.user_id
        items.append(item)
    return {"items": items, "page": page, "limit": limit, "total": total}


# Declared before "/{negotiation_id}" routes so it is not read as an id.
@router.get("/unread")
def unread_summary(
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """How many of the caller's own conversations have broker messages they have not opened."""
    mine = session.query(models.Negotiation).filter(
        models.Negotiation.company_id == company_id,
        models.Negotiation.dispatcher_user_id == principal.user_id,
        service.has_unread(principal.user_id),
    )
    return {"mine": mine.count()}


@router.post("/{negotiation_id}/read")
def mark_read(
    negotiation_id: str,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    try:
        negotiation = service.get_for_company(session, negotiation_id, company_id)
    except NegotiationError as e:
        raise _http(e)
    read_at = service.mark_read(session, negotiation, principal.user_id)
    return {"negotiation_id": str(negotiation.id), "read_at": read_at}


@router.post("/{negotiation_id}/messages", status_code=201)
async def write_message(
    negotiation_id: str,
    body: schemas.WriteMessageRequest,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """Email the broker on the thread. 403 `not_your_conversation` for anyone but the bid's dispatcher."""
    try:
        negotiation = service.get_for_company(session, negotiation_id, company_id)
        message = await service.write(session, negotiation, principal.user_id, body.body)
    except NegotiationError as e:
        raise _http(e)
    # Writing is reading: the sender has seen everything up to their reply.
    service.mark_read(session, negotiation, principal.user_id)
    return schemas.message_to_out(message)
