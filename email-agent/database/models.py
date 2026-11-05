import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, text,
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
# Closed by the system, not by a person: the conversation moved on and the
# draft was never needed. Distinct from `ignored`, which is a human decision.
SUPERSEDED = "superseded"

# Statuses that no longer ask anything of a dispatcher.
RESOLVED_STATUSES = (IGNORED, SENT, EDITED_SENT, SUPERSEDED)

# suggestion.kind
KIND_REPLY = "reply"
KIND_MISMATCH = "mismatch_reply"
KIND_PARSE_FAILURE = "parse_failure"

# ratecon_check.outcome
OUTCOME_PASSED = "passed"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_PARSE_FAILED = "parse_failed"


TRANSPORT_NYLAS = "nylas"
TRANSPORT_IMAP = "imap"


class EmailAccount(Base):
    """
    A mailbox connected through Nylas: either the company's shared dispatch
    mailbox (`user_id` is null, one per company) or one dispatcher's own
    mailbox (`user_id` set, one per user). A dispatcher with their own
    mailbox bids from it; everyone else bids from the shared one.
    """
    __tablename__ = "email_accounts"
    __table_args__ = (
        Index(
            "uq_email_accounts_company_shared", "company_id",
            unique=True, postgresql_where=text("user_id IS NULL"),
        ),
        Index(
            "uq_email_accounts_user", "user_id",
            unique=True, postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id = _uuid_pk()
    company_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    nylas_grant_id = Column(String, nullable=False, unique=True, index=True)
    email_address = Column(String, nullable=False)
    # The mailbox management named when it generated the auth URL. The
    # callback refuses a grant for anything else, so authorising the wrong
    # account at the provider cannot quietly repoint a company's dispatch
    # mail. Null on accounts connected before the check existed.
    expected_email_address = Column(String, nullable=True)
    status = Column(String, default="active", nullable=False)
    # How mail moves for this mailbox. "nylas": through the grant above.
    # "imap": with an app password, sent over SMTP and read over IMAP by
    # services.shared_inbox; `nylas_grant_id` then holds a placeholder
    # ("imap:<uuid>") because the column is required and unique.
    transport = Column(String, default=TRANSPORT_NYLAS, nullable=False, server_default=TRANSPORT_NYLAS)
    imap_host = Column(String, nullable=True)
    imap_port = Column(Integer, nullable=True)
    smtp_host = Column(String, nullable=True)
    smtp_port = Column(Integer, nullable=True)
    # The app password, encrypted with MAILBOX_SECRET_KEY (services.secrets).
    secret = Column(Text, nullable=True)
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    connected_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    @property
    def uses_imap(self) -> bool:
        return self.transport == TRANSPORT_IMAP

    def __repr__(self):
        return f"<EmailAccount company={self.company_id} user={self.user_id} email={self.email_address}>"


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
    # The mailbox the bid went out from. Every later reply on the thread has
    # to leave from the same one, or the broker sees a stranger answer. Null
    # on negotiations opened before personal mailboxes, which all used the
    # company's shared mailbox.
    email_account_id = Column(UUID(as_uuid=True), nullable=True)

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
    # The RFC 5322 Message-Id the sending client wrote. Unlike the Nylas id it
    # identifies the *email*, not one mailbox copy of it, which is what keeps a
    # reply sent from a mail client from landing on the thread twice — see
    # inbound.duplicate_of(). Not unique: two companies can be sent the same
    # email, and each has its own negotiation to record it on.
    rfc_message_id = Column(String, nullable=True, index=True)
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
    # Plain English, written to be shown to a dispatcher as-is. Set when the
    # system resolves a draft so the panel never has to explain itself.
    resolved_reason = Column(Text, nullable=True)

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


class ConversationRead(Base):
    """
    When a user last opened a conversation. Messages after it, other than the
    ones they sent, are new to them. One row per user and conversation, so a
    manager reading a dispatcher's thread does not mark it read for the
    dispatcher.
    """
    __tablename__ = "conversation_reads"

    negotiation_id = Column(
        UUID(as_uuid=True), ForeignKey("negotiations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id = Column(Integer, primary_key=True)
    read_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class ProcessedWebhook(Base):
    """Nylas retries webhooks; this keeps message handling idempotent."""
    __tablename__ = "processed_webhooks"

    id = _uuid_pk()
    nylas_message_id = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)


class ListenerMailbox(Base):
    """
    The mailbox the Sylectus posted-loads listener reads. One row per company
    (one company today). Set by a developer under Admin > Mailboxes; the
    listener (a separate service sharing this database) reads the newest row
    and signs in with the decrypted app password, falling back to its own
    .env when there is none.
    """
    __tablename__ = "listener_mailbox"

    id = _uuid_pk()
    company_id = Column(Integer, nullable=False, index=True)
    email_address = Column(String, nullable=False)
    imap_host = Column(String, nullable=False, default="imap.gmail.com")
    imap_port = Column(Integer, nullable=False, default=993)
    folder = Column(String, nullable=False, default="INBOX")
    secret = Column(Text, nullable=False)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)


class SharedInboxCursor(Base):
    """
    How far the IMAP reader has got in the shared mailbox: the last UID it
    handled, valid for one UIDVALIDITY of the folder. A cursor whose
    UIDVALIDITY no longer matches is started again from the folder's end,
    since the old UIDs mean nothing any more.
    """
    __tablename__ = "shared_inbox_cursors"

    mailbox = Column(String, primary_key=True)
    uidvalidity = Column(BigInteger, nullable=False)
    last_uid = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)
