"""
In-process fan-out for dispatcher notifications. Mirrors atrek's SSE hub:
one queue per connected client, company filtering at the edge.

Single-instance only. Running more than one replica requires swapping this
for Redis pub/sub — the publish/subscribe surface stays the same.
"""
import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUGGESTION_CREATED = "suggestion_created"
RATECON_MISMATCH = "ratecon_mismatch"
RATECON_PARSE_FAILED = "ratecon_parse_failed"
LOAD_BOOKED = "load_booked"
BOOKING_FAILED = "booking_failed"
NEGOTIATION_UPDATED = "negotiation_updated"

QUEUE_SIZE = 64


@dataclass
class Event:
    type: str
    company_id: int
    negotiation_id: Optional[str] = None
    load_uuid: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("company_id", None)
        return data


class EventHub:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event: Event) -> None:
        """Non-blocking: a client too slow to drain its queue loses events, not the pipeline."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full — dropping event %s", event.type)


hub = EventHub()


def publish(
    event_type: str,
    company_id: int,
    negotiation_id: Optional[str] = None,
    load_uuid: Optional[str] = None,
    **payload,
) -> None:
    hub.publish(
        Event(
            type=event_type,
            company_id=company_id,
            negotiation_id=str(negotiation_id) if negotiation_id else None,
            load_uuid=load_uuid,
            payload=payload,
        )
    )
