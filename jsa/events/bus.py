"""In-process pub/sub event bus: asyncio.Queue per WebSocket client."""

import asyncio
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Any]] = set()

    async def publish(self, event: dict) -> None:
        """Broadcast event to all subscriber queues."""
        for q in list(self._queues):   # snapshot to avoid mutation during iteration
            await q.put(event)

    def subscribe(self) -> asyncio.Queue[Any]:
        """Register a new subscriber. Returns an asyncio.Queue."""
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        """Remove a subscriber queue."""
        self._queues.discard(q)


# Module-level singleton — single process, single user
bus = EventBus()
