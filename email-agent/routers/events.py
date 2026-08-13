"""SSE stream of dispatcher notifications, scoped to the caller's company."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from services import events as hub_module
from services.auth import Principal, current_user_sse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])

HEARTBEAT_SECONDS = 25


@router.get("/stream")
async def stream(
    request: Request,
    principal: Principal = Depends(current_user_sse),
):
    company_id = principal.company_id

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
        finally:
            await hub_module.hub.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
