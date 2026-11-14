"""SSE stream of dispatcher notifications, scoped to the caller's company."""
import asyncio
import json
import logging

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services import events as hub_module
from services import alerts
from services.auth import Principal, current_user_sse, require_internal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])
# Server-to-server: other services (the Django API) put their own events on the
# same stream, so a page gets them without opening another connection.
internal_router = APIRouter(prefix="/internal/v1/events", tags=["internal"])

# Only these types may come from outside: the agent's own events stay its own.
EXTERNAL_TYPES = {hub_module.HIRING_CHANGED}


class PublishRequest(BaseModel):
    type: str
    company_id: int
    payload: dict[str, Any] = Field(default_factory=dict)


@internal_router.post("", status_code=202)
def publish_event(body: PublishRequest, _: bool = Depends(require_internal)) -> dict[str, Any]:
    """POST /internal/v1/events {type, company_id, payload}: fan the event out to the
    company's open streams. Nothing is stored; a page that was not connected catches up
    by reloading what the event is about."""
    if body.type not in EXTERNAL_TYPES:
        raise HTTPException(status_code=400, detail=f"unknown event type {body.type!r}")
    hub_module.publish(body.type, body.company_id, **body.payload)
    return {"published": body.type, "subscribers": hub_module.hub.count()}

HEARTBEAT_SECONDS = 25


def _client_ip(request: Request) -> str:
    # nginx sits in front of this service, so request.client.host is always
    # nginx's own loopback address — the real caller is only in the headers
    # it forwards.
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


@router.get("/stream")
async def stream(
    request: Request,
    principal: Principal = Depends(current_user_sse),
):
    company_id = principal.company_id
    client_ip = _client_ip(request)
    logger.info("SSE stream opened: user=%s company=%s ip=%s", principal.user_id, company_id, client_ip)

    async def generator():
        queue = await hub_module.hub.subscribe()
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                if event.company_id != company_id:
                    continue
                yield f"event: {event.type}\ndata: {json.dumps(event.to_dict(), default=str)}\n\n"
        except Exception as e:  # noqa: BLE001 - reported, then the stream ends
            logger.exception("SSE stream failed: user=%s company=%s", principal.user_id, company_id)
            alerts.stream_failed(f"{type(e).__name__}: {e}")
            raise
        finally:
            await hub_module.hub.unsubscribe(queue)
            logger.info("SSE stream closed: user=%s company=%s ip=%s", principal.user_id, company_id, client_ip)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
