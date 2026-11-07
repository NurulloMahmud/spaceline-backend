"""SSE stream of dispatcher notifications, scoped to the caller's company."""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from config.settings import config
from services import events as hub_module
from services.boxtruck import boxtruck
from services import telegram
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


# user_id -> label, so a dispatcher whose stream reconnects all day does not
# cost a TMS call each time. Usernames rarely change; a restart clears it.
_user_labels: dict[int, str] = {}


async def _user_label(user_id: int) -> str:
    """'username (First Last)' for the alert, or '' when the TMS has no answer."""
    if user_id in _user_labels:
        return _user_labels[user_id]
    try:
        people = await boxtruck.get_dispatchers_bulk([user_id])
    except Exception as e:  # noqa: BLE001 - the alert must still go out
        logger.warning("IP alert user lookup failed for %s: %s", user_id, e)
        return ""
    person = people.get(user_id)
    if not person:
        return ""
    username = person.get("username") or ""
    full = f"{person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
    label = f"{username} ({full})" if username and full else (username or full)
    _user_labels[user_id] = label
    return label


async def _send_ip_alert(principal: Principal, client_ip: str) -> None:
    label = await _user_label(principal.user_id)
    who = f"{label} - id {principal.user_id}" if label else f"id {principal.user_id}"
    text = (
        f"SSE stream opened\n"
        f"user: {who} (company {principal.company_id})\n"
        f"ip: {client_ip}"
    )
    await telegram.send_message(
        config.IP_ALERT_CHAT_ID, text, bot_token=config.IP_ALERT_BOT_TOKEN
    )


def _notify_ip_async(principal: Principal, client_ip: str) -> None:
    # Fire-and-forget: the user lookup and send_message both swallow their own
    # errors, so a slow or failing TMS or Telegram call never delays or breaks
    # the SSE connection it is reporting on.
    asyncio.create_task(_send_ip_alert(principal, client_ip))


@router.get("/stream")
async def stream(
    request: Request,
    principal: Principal = Depends(current_user_sse),
):
    company_id = principal.company_id
    client_ip = _client_ip(request)
    logger.info("SSE stream opened: user=%s company=%s ip=%s", principal.user_id, company_id, client_ip)
    _notify_ip_async(principal, client_ip)

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
