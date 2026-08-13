import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from config.settings import config

logger = logging.getLogger(__name__)

engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    return SessionFactory()


ADDED_COLUMNS = (
    "ALTER TABLE pending_offers ADD COLUMN IF NOT EXISTS telegram_message_id BIGINT",
)


def init_db():
    from database.models import Base
    Base.metadata.create_all(engine)
    # create_all only creates missing tables, never alters existing ones.
    with engine.begin() as conn:
        for statement in ADDED_COLUMNS:
            try:
                conn.execute(text(statement))
            except Exception as e:
                logger.error(f"schema update failed: {statement} — {e}")
    