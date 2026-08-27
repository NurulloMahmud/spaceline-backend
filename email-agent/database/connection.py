import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import config
from database.models import Base

logger = logging.getLogger(__name__)

engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


# Columns added to tables that already exist. create_all() only creates
# missing tables, so a new column on a live database needs saying explicitly.
# Each statement is idempotent, so this is safe on every boot.
ADDITIVE_COLUMNS = (
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS resolved_reason TEXT",
    "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS expected_email_address VARCHAR",
)


def init_db() -> None:
    Base.metadata.create_all(engine)

    with engine.begin() as conn:
        for statement in ADDITIVE_COLUMNS:
            conn.execute(text(statement))

    logger.info("Database schema ready")


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def session_dependency():
    """FastAPI dependency — commits on success, rolls back on exception."""
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
