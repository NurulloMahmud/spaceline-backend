"""SSE stream of dispatcher notifications, scoped to the caller's company."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from services import events as hub_module
from services import alerts
from services.auth import Principal, current_user_sse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])

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
