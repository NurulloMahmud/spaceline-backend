import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import models
from database.connection import session_dependency
from routers import schemas
from services import negotiations as service
from config.settings import config
from services import maintenance
from services import suggestions as suggestion_service
from services.auth import Principal, current_user, require_internal, scoped_company
from services.boxtruck import boxtruck

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/negotiations", tags=["negotiations"])


def _pending_counts(session: Session, negotiation_ids: list) -> dict:
    if not negotiation_ids:
        return {}
    rows = (
        session.query(models.Suggestion.negotiation_id, func.count(models.Suggestion.id))
        .filter(
            models.Suggestion.negotiation_id.in_(negotiation_ids),
            models.Suggestion.status == models.PENDING,
        )
        .group_by(models.Suggestion.negotiation_id)
        .all()
    )
    return {row[0]: row[1] for row in rows}


internal_router = APIRouter(prefix="/internal/v1", tags=["internal"])


@internal_router.get("/driver-holds")
def driver_holds(
    _: bool = Depends(require_internal),
    session: Session = Depends(session_dependency),
):
    """
    Drivers whose bid is currently live with a broker.

    The telegram bot calls this before offering a load: a driver we have just
    bid should not be handed something else while the broker is deciding.
    """
    return {
        "driver_ids": maintenance.held_driver_ids(session),
        "hold_minutes": config.DRIVER_HOLD_MINUTES,
    }


@router.post("", status_code=201)
async def create_negotiation(
    body: schemas.CreateNegotiationRequest,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """
    Start a bid: emails the broker our price and opens the thread.
    422 with code `broker_email_required` means the UI must ask for an address.
    """
    try:
        negotiation = await service.create_negotiation(
            session=session,
            company_id=company_id,
            dispatcher_user_id=principal.user_id,
            load_uuid=body.load_uuid,
            bid_amount=body.bid_amount,
            driver_bid_id=body.driver_bid_id,
            driver_id=body.driver_id,
            driver_amount=body.driver_amount,
            broker_email=str(body.broker_email) if body.broker_email else None,
            note=body.note,
        )
    except service.NegotiationError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": e.message, "code": e.code},
        )

    return schemas.negotiation_to_out(negotiation)


@router.get("")
async def list_negotiations(
    status: str = Query(default=""),
    mine: Optional[bool] = Query(
        default=None,
        description="true: only bids I sent; false: only other dispatchers'; omit for all",
    ),
    dispatcher_user_id: Optional[int] = Query(
        default=None, description="bids sent by a specific dispatcher"
    ),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    query = session.query(models.Negotiation).filter(
        models.Negotiation.company_id == company_id
    )
    if status:
        query = query.filter(models.Negotiation.status.in_(status.split(",")))

    if mine is True:
        query = query.filter(
            models.Negotiation.dispatcher_user_id == principal.user_id
        )
    elif mine is False:
        query = query.filter(
            models.Negotiation.dispatcher_user_id != principal.user_id
        )

    if dispatcher_user_id is not None:
        query = query.filter(
            models.Negotiation.dispatcher_user_id == dispatcher_user_id
        )

    total = query.count()
    rows = (
        query.order_by(models.Negotiation.updated_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    pending = _pending_counts(session, [r.id for r in rows])

    driver_ids = list({r.driver_id for r in rows if r.driver_id is not None})
    dispatcher_ids = list({r.dispatcher_user_id for r in rows if r.dispatcher_user_id is not None})
    drivers = await boxtruck.get_drivers_bulk(driver_ids)
    dispatchers = await boxtruck.get_dispatchers_bulk(dispatcher_ids)

    return {
        "items": [
            schemas.negotiation_to_out(
                r,
                pending.get(r.id, 0),
                driver=drivers.get(r.driver_id),
                dispatcher=dispatchers.get(r.dispatcher_user_id),
            )
            for r in rows
        ],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/{negotiation_id}")
async def get_negotiation(
    negotiation_id: str,
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    negotiation = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.id == negotiation_id,
            models.Negotiation.company_id == company_id,
        )
        .first()
    )
    if not negotiation:
        raise HTTPException(404, detail={"error": "Negotiation not found", "code": "not_found"})

    pending = sum(1 for s in negotiation.suggestions if s.status == models.PENDING)
    driver_ids = [negotiation.driver_id] if negotiation.driver_id is not None else []
    dispatcher_ids = (
        [negotiation.dispatcher_user_id] if negotiation.dispatcher_user_id is not None else []
    )
    drivers = await boxtruck.get_drivers_bulk(driver_ids)
    dispatchers = await boxtruck.get_dispatchers_bulk(dispatcher_ids)
    data = schemas.negotiation_to_out(
        negotiation,
        pending,
        driver=drivers.get(negotiation.driver_id),
        dispatcher=dispatchers.get(negotiation.dispatcher_user_id),
    )
    data["load_snapshot"] = negotiation.load_snapshot or {}
    data["messages"] = [schemas.message_to_out(m) for m in negotiation.messages]
    # The thread carries only what still says something. A sent draft is the
    # same email as the outbound message already in `messages`, and a
    # superseded one is a reply that never went anywhere because the
    # conversation moved on — showing either turned the thread into a list of
    # the dispatcher's own past actions. Pending ones need a decision, and
    # ignored ones record a deliberate choice not to reply, so both stay.
    # Everything remains available on GET /suggestions.
    data["suggestions"] = [
        schemas.suggestion_to_out(s)
        for s in negotiation.suggestions
        if s.status not in (models.SENT, models.EDITED_SENT, models.SUPERSEDED)
    ]
    # Surfaced as a count so the panel can say "3 drafts were closed
    # automatically" without listing nine of them.
    data["superseded_suggestions"] = sum(
        1 for s in negotiation.suggestions if s.status == models.SUPERSEDED
    )
    data["ratecon_checks"] = [
        schemas.ratecon_check_to_out(c) for c in negotiation.ratecon_checks
    ]
    return data


@router.post("/{negotiation_id}/close")
def close_negotiation(
    negotiation_id: str,
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    negotiation = (
        session.query(models.Negotiation)
        .filter(
            models.Negotiation.id == negotiation_id,
            models.Negotiation.company_id == company_id,
        )
        .first()
    )
    if not negotiation:
        raise HTTPException(404, detail={"error": "Negotiation not found", "code": "not_found"})
    if negotiation.status == models.BOOKED:
        raise HTTPException(
            409,
            detail={"error": "A booked negotiation cannot be closed.", "code": "already_booked"},
        )

    negotiation.status = models.CLOSED
    suggestion_service.supersede_pending(
        session, negotiation.id, suggestion_service.NEGOTIATION_CLOSED
    )
    session.flush()
    return schemas.negotiation_to_out(negotiation)
