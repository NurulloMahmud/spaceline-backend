import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import config
from database.models import Base

logger = logging.getLogger(__name__)


def _connect_args() -> dict:
    """
    psycopg2-level guards. Only Postgres understands these, so a non-Postgres
    URL (a throwaway sqlite in a one-off script) gets none of them.

      statement_timeout / idle_in_transaction_session_timeout — server-side
      caps so a slow query or a transaction left open by a stalled handler
      cannot pin a pooled connection forever.

      connect_timeout + TCP keepalives — a dead connection is noticed in
      seconds instead of hanging on a silent socket.
    """
    if not config.DATABASE_URL.startswith(("postgresql", "postgres://")):
        return {}
    return {
        "connect_timeout": config.DB_CONNECT_TIMEOUT_SECONDS,
        "options": (
            f"-c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS} "
            f"-c idle_in_transaction_session_timeout={config.DB_IDLE_TX_TIMEOUT_MS}"
        ),
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=config.DB_POOL_SIZE,
    max_overflow=config.DB_MAX_OVERFLOW,
    # A checkout that cannot be satisfied fails the one request that asked for
    # it after this long, instead of blocking the whole event loop until a
    # connection frees up.
    pool_timeout=config.DB_POOL_TIMEOUT_SECONDS,
    pool_recycle=config.DB_POOL_RECYCLE_SECONDS,
    connect_args=_connect_args(),
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


# Columns added to tables that already exist. create_all() only creates
# missing tables, so a new column on a live database needs saying explicitly.
# Each statement is idempotent, so this is safe on every boot.
ADDITIVE_COLUMNS = (
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS resolved_reason TEXT",
    "ALTER TABLE email_accounts ADD COLUMN IF NOT EXISTS expected_email_address VARCHAR",
    "ALTER TABLE email_messages ADD COLUMN IF NOT EXISTS rfc_message_id VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_email_messages_rfc_message_id "
    "ON email_messages (rfc_message_id)",
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
