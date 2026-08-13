import asyncio
import logging
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    handlers=[logging.StreamHandler(sys.stderr)],
)

logger = logging.getLogger(__name__)

from database.connection import init_db  # noqa: E402
from routers import accounts, events, negotiations, suggestions, webhooks  # noqa: E402
from services import maintenance  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()

    sweeper = asyncio.create_task(maintenance.sweep_forever())
    logger.info("email-agent ready on port %s", config.PORT)
    try:
        yield
    finally:
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper


app = FastAPI(
    title="email-agent",
    description=(
        "Broker email negotiation for the box-truck TMS: sends dispatcher bids, "
        "drafts replies for approval, verifies rate confirmations and books "
        "verified loads."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Internal-Secret"],
    )

app.include_router(negotiations.router)
app.include_router(negotiations.internal_router)
app.include_router(suggestions.router)
app.include_router(accounts.router)
app.include_router(events.router)
app.include_router(webhooks.router)


@app.get("/api/v1/health", tags=["health"])
def health():
    return {"success": True, "message": "email-agent available"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=not config.is_production())
