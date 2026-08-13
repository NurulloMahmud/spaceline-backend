import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import models
from database.connection import session_dependency
from routers import schemas
from services import suggestions as service
from services.auth import Principal, current_user, scoped_company
from services.negotiations import NegotiationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/suggestions", tags=["suggestions"])


def _http(e: NegotiationError) -> HTTPException:
    return HTTPException(status_code=e.status_code, detail={"error": e.message, "code": e.code})


@router.get("")
def list_suggestions(
    status: str = Query(default=models.PENDING),
    kind: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=100),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """The dispatcher's inbox: drafts and alerts awaiting a decision."""
    query = (
        session.query(models.Suggestion)
        .join(models.Negotiation)
        .filter(models.Negotiation.company_id == company_id)
    )
    if status:
        query = query.filter(models.Suggestion.status.in_(status.split(",")))
    if kind:
        query = query.filter(models.Suggestion.kind.in_(kind.split(",")))

    total = query.count()
    rows = (
        query.order_by(models.Suggestion.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    items = []
    for s in rows:
        item = schemas.suggestion_to_out(s)
        item["negotiation"] = schemas.negotiation_to_out(s.negotiation)
        items.append(item)

    return {"items": items, "page": page, "limit": limit, "total": total}


@router.get("/{suggestion_id}")
def get_suggestion(
    suggestion_id: str,
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    try:
        suggestion = service.get_for_company(session, suggestion_id, company_id)
    except NegotiationError as e:
        raise _http(e)

    item = schemas.suggestion_to_out(suggestion)
    item["negotiation"] = schemas.negotiation_to_out(suggestion.negotiation)
    return item


@router.post("/{suggestion_id}/ignore")
def ignore_suggestion(
    suggestion_id: str,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    try:
        suggestion = service.ignore(session, suggestion_id, company_id, principal.user_id)
    except NegotiationError as e:
        raise _http(e)
    return schemas.suggestion_to_out(suggestion)


@router.post("/{suggestion_id}/send")
async def send_suggestion(
    suggestion_id: str,
    body: schemas.SendSuggestionRequest,
    principal: Principal = Depends(current_user),
    company_id: int = Depends(scoped_company),
    session: Session = Depends(session_dependency),
):
    """Send the draft as-is, or send edited text by supplying `body`."""
    try:
        suggestion = await service.send(
            session=session,
            suggestion_id=suggestion_id,
            company_id=company_id,
            user_id=principal.user_id,
            body=body.body,
            subject=body.subject,
        )
    except NegotiationError as e:
        raise _http(e)
    return schemas.suggestion_to_out(suggestion)
