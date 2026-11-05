import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _base_url(name: str, default: str = "") -> str:
    """
    Origin only — this service's clients build the full path themselves.

    agent_bot stores these same hosts *with* an /api or /api/v1 suffix because
    its clients append bare paths, so a copied value would otherwise produce
    /api/api/... and 404 on every call.
    """
    raw = os.getenv(name, default).strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if raw.endswith(suffix):
            trimmed = raw[: -len(suffix)]
            logging.getLogger(__name__).warning(
                "%s was set to %r; using %r — this setting takes the origin only.",
                name, raw, trimmed,
            )
            return trimmed
    return raw


class Config:
    # service
    APP_NAME: str = os.getenv("APP_NAME", "email-agent")
    ENV: str = os.getenv("ENV", "development")
    PORT: int = _int("PORT", 8100)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://postgres:postgres@localhost:5432/email_agent",
    )

    # Database resilience. This service runs a single event loop and talks to
    # Postgres with blocking psycopg2, so any wait that is not bounded can
    # freeze every request, webhook and the sweeper at once until the process
    # is killed. Every wait below is therefore capped.
    #   POOL_TIMEOUT      — how long a connection checkout may block the loop
    #                       before giving up (was the SQLAlchemy default of 30s)
    #   STATEMENT_TIMEOUT — Postgres aborts any single query that runs longer
    #   IDLE_TX_TIMEOUT   — Postgres drops a transaction left open between
    #                       queries this long, so a connection pinned by a
    #                       stalled handler returns to the pool on its own
    DB_POOL_SIZE: int = _int("DB_POOL_SIZE", 10)
    DB_MAX_OVERFLOW: int = _int("DB_MAX_OVERFLOW", 20)
    DB_POOL_TIMEOUT_SECONDS: int = _int("DB_POOL_TIMEOUT_SECONDS", 10)
    DB_POOL_RECYCLE_SECONDS: int = _int("DB_POOL_RECYCLE_SECONDS", 1800)
    DB_CONNECT_TIMEOUT_SECONDS: int = _int("DB_CONNECT_TIMEOUT_SECONDS", 10)
    DB_STATEMENT_TIMEOUT_MS: int = _int("DB_STATEMENT_TIMEOUT_MS", 15000)
    DB_IDLE_TX_TIMEOUT_MS: int = _int("DB_IDLE_TX_TIMEOUT_MS", 60000)

    # auth — JWT_SECRET must match Django's SIMPLE_JWT signing key exactly
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
    INTERNAL_SECRET_KEY: str = os.getenv("INTERNAL_SECRET_KEY", "")

    # peer services — origin only, no /api suffix (see _base_url)
    BOXTRUCK_BASE_URL: str = _base_url("BOXTRUCK_BASE_URL")
    BOXTRUCK_INTERNAL_SECRET: str = os.getenv("BOXTRUCK_INTERNAL_SECRET", "")
    ATREK_BASE_URL: str = _base_url("ATREK_BASE_URL")
    ATREK_INTERNAL_SECRET: str = os.getenv("ATREK_INTERNAL_SECRET", "")

    # Test servers only: when set, every outgoing email goes to this address
    # instead of the real recipient, so a test bid can never reach a broker.
    EMAIL_REDIRECT_TO: str = os.getenv("EMAIL_REDIRECT_TO", "").strip()

    # Whose bids must leave from a mailbox of their own (connected for them
    # under Admin > Mailboxes): comma-separated BTM user ids, or "*" for
    # everyone. Anyone else sends from the company's shared mailbox.
    PERSONAL_MAILBOX_USERS: str = os.getenv("PERSONAL_MAILBOX_USERS", "").strip()

    def personal_mailbox_allowed(self, user_id: int) -> bool:
        allowed = self.PERSONAL_MAILBOX_USERS
        if allowed == "*":
            return True
        return str(user_id) in {part.strip() for part in allowed.split(",") if part.strip()}

    # The company's shared mailbox, read over IMAP with an app password as the
    # catch-all: replies on negotiations that predate per-dispatcher mailboxes
    # and anything copied to it. Both values empty leaves that reader off; the
    # dispatchers' own mailboxes are read regardless.
    SHARED_INBOX_ADDRESS: str = os.getenv("SHARED_INBOX_ADDRESS", "").strip().lower()
    SHARED_INBOX_APP_PASSWORD: str = os.getenv("SHARED_INBOX_APP_PASSWORD", "")
    SHARED_INBOX_IMAP_HOST: str = os.getenv("SHARED_INBOX_IMAP_HOST", "imap.gmail.com")
    SHARED_INBOX_FOLDER: str = os.getenv("SHARED_INBOX_FOLDER", "INBOX")
    SHARED_INBOX_POLL_SECONDS: int = _int("SHARED_INBOX_POLL_SECONDS", 15)

    def shared_inbox_enabled(self) -> bool:
        return bool(self.SHARED_INBOX_ADDRESS and self.SHARED_INBOX_APP_PASSWORD)

    # Admin > Mailboxes (routers/mailboxes.py) is open to management and
    # developers plus these BTM user ids; the app passwords it stores rest
    # encrypted with this Fernet key (services/secrets.py).
    MAILBOX_ADMIN_USERS: str = os.getenv("MAILBOX_ADMIN_USERS", "").strip()
    MAILBOX_SECRET_KEY: str = os.getenv("MAILBOX_SECRET_KEY", "").strip()

    def mailbox_admin(self, user_id: int) -> bool:
        return str(user_id) in {p.strip() for p in self.MAILBOX_ADMIN_USERS.split(",") if p.strip()}

    # openai
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    # A single model request that hangs past this many seconds is abandoned.
    # The SDK default is 600s; with the client running on the event loop that
    # is ten minutes of every send, webhook and sweep frozen behind it.
    OPENAI_TIMEOUT_SECONDS: int = _int("OPENAI_TIMEOUT_SECONDS", 30)
    # SDK-level retries on top of the two attempts _json_call already makes.
    OPENAI_MAX_RETRIES: int = _int("OPENAI_MAX_RETRIES", 1)

    # telegram — same bot as agent_bot, used to notify driver groups
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")

    # separate bot, used only to alert an admin of the client IP opening each
    # SSE connection to /api/v1/events/stream
    IP_ALERT_BOT_TOKEN: str = os.getenv("IP_ALERT_BOT_TOKEN", "")
    IP_ALERT_CHAT_ID: str = os.getenv("IP_ALERT_CHAT_ID", "")

    # behaviour
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]
    # A ratecon whose total differs from the agreed rate by more than this many
    # cents blocks booking. Kept at 0: price must match exactly.
    PRICE_TOLERANCE_CENTS: int = _int("PRICE_TOLERANCE_CENTS", 0)

    # A bid the broker never answered is closed automatically, so the load
    # stops occupying the board and the driver stops being held.
    STALE_BID_MINUTES: int = _int("STALE_BID_MINUTES", 30)
    SWEEP_INTERVAL_SECONDS: int = _int("SWEEP_INTERVAL_SECONDS", 60)

    # While a driver's bid is live with a broker, the telegram bot stops
    # offering them other loads.
    DRIVER_HOLD_MINUTES: int = _int("DRIVER_HOLD_MINUTES", 5)

    def is_production(self) -> bool:
        return self.ENV.lower() in ("production", "prod")


config = Config()
