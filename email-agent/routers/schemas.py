from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class CreateNegotiationRequest(BaseModel):
    load_uuid: str = Field(..., description="atrek load UUID")
    bid_amount: float = Field(..., gt=0, description="price we offer the broker")
    driver_bid_id: Optional[str] = Field(None, description="atrek load_bids row id")
    driver_id: Optional[int] = None
    driver_amount: Optional[float] = Field(None, description="the driver's own bid")
    broker_email: Optional[EmailStr] = Field(
        None, description="required only when the load carries no contact email"
    )
    note: str = ""


class SendSuggestionRequest(BaseModel):
    body: Optional[str] = Field(None, description="omit to send the draft unchanged")
    subject: Optional[str] = None


class ConnectAccountRequest(BaseModel):
    company_id: Optional[int] = Field(
        None, description="management only; defaults to the caller's company"
    )


class MessageOut(BaseModel):
    id: str
    direction: str
    from_email: Optional[str]
    to_email: Optional[str]
    subject: Optional[str]
    body_text: Optional[str]
    has_attachments: bool
    attachments: Optional[list[dict[str, Any]]]
    sent_by_user_id: Optional[int]
    # True when this reply left the mailbox from someone's own mail client
    # instead of through this app, so the thread can show it as such.
    sent_outside_app: bool
    created_at: datetime


class SuggestionOut(BaseModel):
    id: str
    negotiation_id: str
    kind: str
    intent: Optional[str]
    draft_subject: Optional[str]
    draft_body: Optional[str]
    ai_reasoning: Optional[str]
    status: str
    final_body: Optional[str]
    resolved_by_user_id: Optional[int]
    created_at: datetime


class RateconCheckOut(BaseModel):
    id: str
    attachment_filename: Optional[str]
    agreed_amount: Optional[float]
    ratecon_amount: Optional[float]
    price_ok: bool
    locations_ok: bool
    dates_ok: bool
    discrepancies: Optional[list[str]]
    outcome: str
    error: Optional[str]
    parsed_data: Optional[dict[str, Any]]
    created_at: datetime


class NegotiationOut(BaseModel):
    id: str
    company_id: int
    load_uuid: str
    driver_id: Optional[int]
    driver_amount: Optional[float]
    dispatcher_user_id: Optional[int]
    bid_amount: float
    agreed_amount: Optional[float]
    broker_email: str
    broker_name: Optional[str]
    subject: Optional[str]
    status: str
    tms_load_id: Optional[int]
    failure_reason: Optional[str]
    created_at: datetime
    updated_at: datetime
    pending_suggestions: int = 0


class NegotiationDetailOut(NegotiationOut):
    load_snapshot: dict[str, Any]
    messages: list[MessageOut]
    suggestions: list[SuggestionOut]
    ratecon_checks: list[RateconCheckOut]


class Page(BaseModel):
    items: list[Any]
    page: int
    limit: int
    total: int


def negotiation_to_out(n, pending: int = 0) -> dict:
    return {
        "id": str(n.id),
        "company_id": n.company_id,
        "load_uuid": n.load_uuid,
        "driver_id": n.driver_id,
        "driver_amount": n.driver_amount,
        "dispatcher_user_id": n.dispatcher_user_id,
        "bid_amount": n.bid_amount,
        "agreed_amount": n.agreed_amount,
        "broker_email": n.broker_email,
        "broker_name": n.broker_name,
        "subject": n.subject,
        "status": n.status,
        "tms_load_id": n.tms_load_id,
        "failure_reason": n.failure_reason,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
        "pending_suggestions": pending,
    }


def message_to_out(m) -> dict:
    return {
        "id": str(m.id),
        "direction": m.direction,
        "from_email": m.from_email,
        "to_email": m.to_email,
        "subject": m.subject,
        "body_text": m.body_text,
        "has_attachments": m.has_attachments,
        "attachments": m.attachments,
        "sent_by_user_id": m.sent_by_user_id,
        "sent_outside_app": m.direction == "outbound" and m.sent_by_user_id is None,
        "created_at": m.created_at,
    }


def suggestion_to_out(s) -> dict:
    return {
        "id": str(s.id),
        "negotiation_id": str(s.negotiation_id),
        "kind": s.kind,
        "intent": s.intent,
        "draft_subject": s.draft_subject,
        "draft_body": s.draft_body,
        "ai_reasoning": s.ai_reasoning,
        "status": s.status,
        "final_body": s.final_body,
        "resolved_by_user_id": s.resolved_by_user_id,
        "created_at": s.created_at,
    }


def ratecon_check_to_out(c) -> dict:
    return {
        "id": str(c.id),
        "attachment_filename": c.attachment_filename,
        "agreed_amount": c.agreed_amount,
        "ratecon_amount": c.ratecon_amount,
        "price_ok": c.price_ok,
        "locations_ok": c.locations_ok,
        "dates_ok": c.dates_ok,
        "discrepancies": c.discrepancies,
        "outcome": c.outcome,
        "error": c.error,
        "parsed_data": c.parsed_data,
        "created_at": c.created_at,
    }
