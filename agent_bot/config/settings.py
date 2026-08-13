import os
from dotenv import load_dotenv
load_dotenv()


class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    BOXTRUCK_BASE_URL: str = os.getenv("BOXTRUCK_BASE_URL", "")
    BOXTRUCK_INTERNAL_SECRET: str = os.getenv("BOXTRUCK_INTERNAL_SECRET", "")
    AGENT_BASE_URL: str = os.getenv("AGENT_BASE_URL", "")
    AGENT_INTERNAL_SECRET: str = os.getenv("AGENT_INTERNAL_SECRET", "")
    EMAIL_AGENT_BASE_URL: str = os.getenv("EMAIL_AGENT_BASE_URL", "")
    EMAIL_AGENT_INTERNAL_SECRET: str = os.getenv("EMAIL_AGENT_INTERNAL_SECRET", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/telegram_agent")


config = Config()