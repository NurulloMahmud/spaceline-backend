import uuid
from datetime import datetime
from sqlalchemy import BigInteger, Column, Integer, String, Boolean, DateTime, JSON, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DriverPreference(Base):
    __tablename__ = 'driver_preferences'

    driver_id          = Column(Integer, primary_key=True)
    chat_id            = Column(String, nullable=False)
    full_name          = Column(String, nullable=True)
    max_miles          = Column(Integer, nullable=True)
    local_only         = Column(Boolean, default=False, nullable=False)
    available          = Column(Boolean, default=True, nullable=False)
    preference_note    = Column(String, nullable=True)
    preference_expires = Column(DateTime(timezone=True), nullable=True)
    updated_at         = Column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    def __repr__(self):
        return f"<DriverPreference driver_id={self.driver_id} available={self.available}>"


class PendingOffer(Base):
    __tablename__ = 'pending_offers'

    id            = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    driver_id     = Column(Integer, nullable=False, index=True)
    load_id       = Column(String, nullable=False)
    load_snapshot = Column(JSON, nullable=False)
    status        = Column(String, default='sent', nullable=False)
    # The telegram message the offer was sent as. A driver bids by replying to
    # it, which is how we tell a bid apart from ordinary group chatter.
    telegram_message_id = Column(BigInteger, nullable=True, index=True)
    created_at    = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    expires_at    = Column(DateTime(timezone=True), nullable=False)
    responded_at  = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<PendingOffer id={self.id} driver_id={self.driver_id} status={self.status}>"
    