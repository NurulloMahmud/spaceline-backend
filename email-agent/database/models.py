import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )


def _now():
    return datetime.now(timezone.utc)


# negotiation.status
BID_SENT = "bid_sent"
NEGOTIATING = "negotiating"
RATECON_RECEIVED = "ratecon_received"
BOOKED = "booked"
MISMATCH = "mismatch"
FAILED = "failed"
CLOSED = "closed"

# suggestion.status
PENDING = "pending"
IGNORED = "ignored"
SENT = "sent"
EDITED_SENT = "edited_sent"

# suggestion.kind
KIND_REPLY = "reply"
KIND_MISMATCH = "mismatch_reply"
KIND_PARSE_FAILURE = "parse_failure"

# ratecon_check.outcome
OUTCOME_PASSED = "passed"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_PARSE_FAILED = "parse_failed"


class EmailAccount(Base):
    """One shared dispatch mailbox per company, connected through Nylas."""
    __tablename__ = "email_accounts"

    id = _uuid_pk()
    company_id = Column(Integer, nullable=False, unique=True, index=True)
    nylas_grant_id = Column(String, nullable=False, unique=True, index=True)
    email_address = Column(String, nullable=False)
    status = Column(String, default="active", nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    def __repr__(self):
        return f"<EmailAccount company={self.company_id} email={self.email_address}>"


class Negotiation(Base):
    """One dispatcher bid on one load, and the broker email thread it opened."""
    __tablename__ = "negotiations"

    id = _uuid_pk()
    company_id = Column(Integer, nullable=False, index=True)

    load_uuid = Column(String, nullable=False, index=True)
    load_snapshot = Column(JSONB, nullable=False, default=dict)

    driver_bid_id = Column(String, nullable=True)
    driver_id = Column(Integer, nullable=True, index=True)
    driver_amount = Column(Float, nullable=True)
    driver_telegram_group_id = Column(String, nullable=True)

    dispatcher_user_id = Column(Integer, nullable=True, index=True)
    bid_amount = Column(Float, nullable=False)
    # Updated when the AI reads a different agreed rate out of the thread.
    agreed_amount = Column(Float, nullable=True)

    broker_email = Column(String, nullable=False)
    broker_name = Column(String, nullable=True)
    broker_mc = Column(String, nullable=True)
    tms_broker_id = Column(Integer, nullable=True)

    nylas_thread_id = Column(String, nullable=True, index=True)
    subject = Column(String, nullable=True)

    status = Column(String, default=BID_SENT, nullable=False, index=True)
    tms_load_id = Column(Integer, nullable=True)
    failure_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    messages = relationship(
        "EmailMessage", back_populates="negotiation",
        cascade="all, delete-orphan", order_by="EmailMessage.created_at",
    )
    suggestions = relationship(
        "Suggestion", back_populates="negotiation",
        cascade="all, delete-orphan", order_by="Suggestion.created_at",
    )
    ratecon_checks = relationship(
        "RateconCheck", back_populates="negotiation",
        cascade="all, delete-orphan", order_by="RateconCheck.created_at",
    )

    def effective_amount(self) -> float:
        """The rate a ratecon must match."""
        return self.agreed_amount if self.agreed_amount is not None else self.bid_amount

    def __repr__(self):
        return f"<Negotiation {self.id} load={self.load_uuid} status={self.status}>"


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id = _uuid_pk()
    negotiation_id = Column(
        UUID(as_uuid=True), ForeignKey("negotiations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    nylas_message_id = Column(String, nullable=True, unique=True, index=True)
    direction = Column(String, nullable=False)  # inbound | outbound
    from_email = Column(String, nullable=True)
    to_email = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    body_text = Column(Text, nullable=True)
    has_attachments = Column(Boolean, default=False, nullable=False)
    attachments = Column(JSONB, nullable=True)
    sent_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    negotiation = relationship("Negotiation", back_populates="messages")

    def __repr__(self):
        return f"<EmailMessage {self.id} {self.direction}>"


class Suggestion(Base):
    """
    A drafted reply awaiting a dispatcher decision. Nothing here is ever sent
    to a broker without an explicit send action.
    """
    __tablename__ = "suggestions"

    id = _uuid_pk()
    negotiation_id = Column(
        UUID(as_uuid=True), ForeignKey("negotiations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    in_reply_to_message_id = Column(UUID(as_uuid=True), nullable=True)
    kind = Column(String, default=KIND_REPLY, nullable=False)
    intent = Column(String, nullable=True)

    draft_subject = Column(String, nullable=True)
    draft_body = Column(Text, nullable=True)
    ai_reasoning = Column(Text, nullable=True)

    status = Column(String, default=PENDING, nullable=False, index=True)
    final_body = Column(Text, nullable=True)
    resolved_by_user_id = Column(Integer, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    negotiation = relationship("Negotiation", back_populates="suggestions")

    def __repr__(self):
        return f"<Suggestion {self.id} kind={self.kind} status={self.status}>"


class RateconCheck(Base):
    """The verdict on one rate confirmation attachment."""
    __tablename__ = "ratecon_checks"

    id = _uuid_pk()
    negotiation_id = Column(
        UUID(as_uuid=True), ForeignKey("negotiations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    email_message_id = Column(UUID(as_uuid=True), nullable=True)
    attachment_filename = Column(String, nullable=True)

    parsed_data = Column(JSONB, nullable=True)
    agreed_amount = Column(Float, nullable=True)
    ratecon_amount = Column(Float, nullable=True)

    price_ok = Column(Boolean, default=False, nullable=False)
    locations_ok = Column(Boolean, default=False, nullable=False)
    dates_ok = Column(Boolean, default=False, nullable=False)
    discrepancies = Column(JSONB, nullable=True)

    outcome = Column(String, nullable=False, index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    negotiation = relationship("Negotiation", back_populates="ratecon_checks")

    def __repr__(self):
        return f"<RateconCheck {self.id} outcome={self.outcome}>"


class ProcessedWebhook(Base):
    """Nylas retries webhooks; this keeps message handling idempotent."""
    __tablename__ = "processed_webhooks"

    id = _uuid_pk()
    nylas_message_id = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
