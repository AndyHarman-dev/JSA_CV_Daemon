"""Phase 6 — streaming protocol/events/accumulator tests.

Covers: a supports_streaming=False backend never receives on_chunk; the
ChunkAccumulator batches at most N events for M tokens; a superseded turn end
follows a retry; a chunk-path exception never propagates.
"""

import asyncio

import pytest

from jsa.agents.base import AgentChunk, AgentReply
from jsa.agents.protocol import ProtocolError
from jsa.db.models import Job, Stage
from jsa.events.bus import bus
from jsa.pipeline.stages import _send_message_with_wire_retry, _start_session_with_retry
from jsa.pipeline.streaming import ChunkAccumulator
from jsa.schema.turn_models import json_schema_for
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle

pytestmark = pytest.mark.asyncio


def _job() -> Job:
    return Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")


def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class TestNonStreamingBackendNeverGetsOnChunk:
    async def test_start_session_no_on_chunk_kwarg_when_not_streaming(self):
        backend = FakeAgentBackend(
            [AgentReply(raw="x", content="x", kind="final")],
            supports_streaming=False,
        )
        called = {"n": 0}

        async def on_chunk(chunk):
            called["n"] += 1

        handle, reply = await _start_session_with_retry(
            backend, "sys", "hi", None, Stage.cv_adjust, _job(), on_chunk=on_chunk, accumulator=None
        )
        assert reply.kind == "final"
        assert called["n"] == 0


class TestAccumulatorBatching:
    async def test_flushes_at_most_bounded_events_for_many_tokens(self):
        queue = bus.subscribe()
        try:
            acc = ChunkAccumulator("job1", "cv_adjust")
            for _ in range(50):
                await acc.add_chunk(AgentChunk(kind="content", text="ab"))
            await acc.end_turn(superseded=False)
            events = _drain(queue)
            chunk_events = [e for e in events if e["type"] == "agent_chunk"]
            end_events = [e for e in events if e["type"] == "agent_turn_end"]
            # 50 * 2 = 100 chars, under the 200-char size threshold, so nothing
            # flushes mid-loop; only the force-flush at end_turn publishes.
            assert len(chunk_events) <= 3
            assert len(end_events) == 1
            assert end_events[0]["superseded"] is False
            assert "".join(e["text"] for e in chunk_events) == "ab" * 50
        finally:
            bus.unsubscribe(queue)

    async def test_size_threshold_flush(self):
        queue = bus.subscribe()
        try:
            acc = ChunkAccumulator("job1", "cv_adjust")
            await acc.add_chunk(AgentChunk(kind="content", text="x" * 250))
            events = _drain(queue)
            assert any(e["type"] == "agent_chunk" for e in events)
        finally:
            bus.unsubscribe(queue)

    async def test_reset_discards_buffer_without_publishing(self):
        queue = bus.subscribe()
        try:
            acc = ChunkAccumulator("job1", "cv_adjust")
            await acc.add_chunk(AgentChunk(kind="content", text="partial"))
            acc.reset()
            await acc.end_turn(superseded=False)
            events = _drain(queue)
            chunk_events = [e for e in events if e["type"] == "agent_chunk"]
            assert chunk_events == []
        finally:
            bus.unsubscribe(queue)


class _FlakyOnceBackend(FakeAgentBackend):
    """Raises ProtocolError on the first send_message, succeeds on the second."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._raised = False

    async def send_message(self, handle, text, structured_schema=None, on_chunk=None):
        if not self._raised:
            self._raised = True
            raise ProtocolError("bad")
        return await super().send_message(handle, text, structured_schema, on_chunk)


class TestSupersededOnRetry:
    async def test_send_message_wire_retry_emits_superseded_turn_end(self):
        backend = _FlakyOnceBackend(
            [AgentReply(raw="ok", content="ok", kind="needs_input", question="q?")],
            supports_structured_output=True,
        )
        handle = FakeSessionHandle(id="h1")
        schema = json_schema_for(Stage.cv_adjust)
        acc = ChunkAccumulator("job1", "cv_adjust")
        queue = bus.subscribe()
        try:
            reply, msgs = await _send_message_with_wire_retry(
                backend, handle, "hi", schema, Stage.cv_adjust, _job(), accumulator=acc
            )
            assert reply.kind == "needs_input"
            events = _drain(queue)
            end_events = [e for e in events if e["type"] == "agent_turn_end"]
            assert any(e["superseded"] is True for e in end_events)
        finally:
            bus.unsubscribe(queue)


class TestBestEffort:
    async def test_on_chunk_exception_is_swallowed(self):
        acc = ChunkAccumulator("job1", "cv_adjust")

        async def broken_flush():
            raise RuntimeError("boom")

        acc._flush = broken_flush  # type: ignore[method-assign]
        # add_chunk must not raise even though the underlying flush path is broken.
        await acc.add_chunk(AgentChunk(kind="content", text="x" * 300))
