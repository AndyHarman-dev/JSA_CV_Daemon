"""WebSocket endpoint: /ws — fan-out all bus events to connected clients."""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from jsa.events.bus import bus

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    queue = bus.subscribe()

    async def _reader() -> None:
        """Drain incoming frames to detect disconnect."""
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    async def _writer() -> None:
        """Forward events from bus queue to the WebSocket."""
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event))

    reader_task = asyncio.create_task(_reader())
    writer_task = asyncio.create_task(_writer())
    try:
        done, pending = await asyncio.wait(
            [reader_task, writer_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        bus.unsubscribe(queue)
