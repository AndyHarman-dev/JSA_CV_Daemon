"""ChunkAccumulator: batches streamed AgentChunks before they reach the event bus.

Batching lives here (in the pipeline layer), NOT in EventBus itself and NOT in any
backend -- backends only ever see the plain ``on_chunk`` callable. One accumulator is
created per ``run_stage`` invocation (i.e. per job+stage attempt) by
``jsa/pipeline/stages.py`` and threaded into whichever backend call(s) actually run.

Streaming is best-effort everywhere it touches this accumulator: any exception raised
while handling a chunk is swallowed here so a streaming bug can never fail a job -- see
CLAUDE.md-equivalent rule for the agent-chat-upgrade plan, Phase 6.
"""

from __future__ import annotations

import asyncio
import logging

from jsa.agents.base import AgentChunk
from jsa.events.bus import bus
from jsa.events.schema import AgentChunkEvent, AgentTurnEndEvent, event_to_dict

logger = logging.getLogger(__name__)

# ~75ms flush timer, justified by the SSE backends (claude-cli's own channel is
# already coarse -- see the plan's Phase 6 rationale). Size threshold is a second,
# independent trigger so a burst of chunks doesn't wait out the whole timer.
_FLUSH_INTERVAL_SECONDS = 0.075
_FLUSH_SIZE_THRESHOLD = 200  # characters, per kind


class ChunkAccumulator:
    """Coalesces AgentChunks for one job+stage turn, flushing batched
    AgentChunkEvents to the bus on a timer or size threshold, whichever first.

    Also accumulates the FULL joined reasoning text for the turn (independent of
    flush timing) so the caller can persist it once the turn completes -- see
    ``take_reasoning()``.
    """

    def __init__(self, job_id: str, stage: str) -> None:
        self._job_id = job_id
        self._stage = stage
        self._buffers: dict[str, str] = {"content": "", "reasoning": ""}
        self._full_reasoning: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def add_chunk(self, chunk: AgentChunk) -> None:
        """The callable handed to backends as ``on_chunk``. Best-effort: never
        raises out of this method."""
        try:
            async with self._lock:
                self._buffers[chunk.kind] = self._buffers.get(chunk.kind, "") + chunk.text
                if chunk.kind == "reasoning":
                    self._full_reasoning.append(chunk.text)
                should_flush_now = len(self._buffers[chunk.kind]) >= _FLUSH_SIZE_THRESHOLD
            if should_flush_now:
                await self._flush()
            else:
                self._schedule_flush()
        except Exception:  # noqa: BLE001 - streaming must never fail the job
            logger.exception(
                "streaming: failed to buffer a chunk for job %s stage %s", self._job_id, self._stage
            )

    def _schedule_flush(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._delayed_flush())
        except Exception:  # noqa: BLE001
            logger.exception("streaming: failed to schedule flush timer")

    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            await self._flush()
        except Exception:  # noqa: BLE001
            logger.exception("streaming: delayed flush failed")

    async def _flush(self) -> None:
        try:
            async with self._lock:
                pending = {k: v for k, v in self._buffers.items() if v}
                self._buffers = {"content": "", "reasoning": ""}
            for kind, text in pending.items():
                await bus.publish(
                    event_to_dict(
                        AgentChunkEvent(job_id=self._job_id, stage=self._stage, kind=kind, text=text)
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception(
                "streaming: failed to flush chunks for job %s stage %s", self._job_id, self._stage
            )

    async def force_flush(self) -> None:
        """Flush any pending buffer immediately. MUST be called before the pipeline
        publishes AgentTurnEndEvent, or the final batch races the buffer clear."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
        await self._flush()

    def reset(self) -> None:
        """Reset hook for a fresh attempt of the SAME turn (e.g. an in-backend
        retry_transient loop, or a nudge replay) -- discards any partial buffer
        WITHOUT publishing it, so the next attempt doesn't concatenate onto a
        failed attempt's partial output. Synchronous and cheap; safe to call from
        any retry loop right before re-issuing the call."""
        self._buffers = {"content": "", "reasoning": ""}
        self._full_reasoning = []
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()

    async def end_turn(self, *, superseded: bool = False) -> None:
        """Force-flush, then publish AgentTurnEndEvent. ``superseded=True`` marks a
        turn whose streamed buffer must be discarded by the frontend (a replay
        path produced a superseding second assistant turn)."""
        try:
            await self.force_flush()
            await bus.publish(
                event_to_dict(
                    AgentTurnEndEvent(job_id=self._job_id, stage=self._stage, superseded=superseded)
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "streaming: failed to end turn for job %s stage %s", self._job_id, self._stage
            )

    def take_reasoning(self) -> str | None:
        """The full joined reasoning text accumulated so far, or None if empty.
        Does not clear the buffer counters used for flush batching (those are
        cleared by ``_flush``) -- only the dedicated full-reasoning list."""
        text = "".join(self._full_reasoning)
        self._full_reasoning = []
        return text or None
