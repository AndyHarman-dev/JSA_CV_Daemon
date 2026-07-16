"""In-process pub/sub event bus: asyncio.Queue per WebSocket client."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Bound on each subscriber's queue. This is a local, single-user tool — the
# bound just needs to stop unbounded memory growth from a stalled-but-still-
# connected WebSocket consumer (one whose reader loop in jsa/api/ws.py is slow
# to drain, e.g. a slow client network); it doesn't need fine tuning.
_DEFAULT_MAXSIZE = 300


class EventBus:
    def __init__(self, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue[Any]] = set()

    async def publish(self, event: dict) -> None:
        """Broadcast event to all subscriber queues.

        Each queue is bounded (see subscribe()). If a subscriber's queue is
        full — i.e. a consumer is stalled and not draining it — we drop the
        NEW event for that subscriber (drop-newest) rather than blocking the
        publisher or letting the queue grow unbounded. Drop-newest is chosen
        over drop-oldest because it's race-free with a plain put_nowait(): no
        get_nowait()/put_nowait() pair that could interleave with a concurrent
        consumer draining the same queue.
        """
        for q in list(self._queues):   # snapshot to avoid mutation during iteration
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "EventBus: subscriber queue full (maxsize=%d); dropping event %r",
                    self._maxsize,
                    event.get("type", event),
                )

    def subscribe(self) -> asyncio.Queue[Any]:
        """Register a new subscriber. Returns a bounded asyncio.Queue."""
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        """Remove a subscriber queue."""
        self._queues.discard(q)


# Module-level singleton — single process, single user
bus = EventBus()
