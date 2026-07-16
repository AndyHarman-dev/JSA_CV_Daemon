"""FakeAgentBackend — scripted, deterministic AgentBackend for testing."""

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, SessionHandle


@dataclass
class FakeSessionHandle(SessionHandle):
    """Concrete SessionHandle for FakeAgentBackend."""
    # id and external_id are inherited from SessionHandle


class FakeAgentBackend(AgentBackend):
    """Scripted AgentBackend that returns pre-configured replies in sequence.

    Usage::

        replies = [
            AgentReply(raw="...", content="CV text", kind="final"),
            AgentReply(raw="...", content="Cover letter", kind="final"),
        ]
        backend = FakeAgentBackend(replies)
        handle, reply = await backend.start_session(system_prompt, user_msg)
        # reply is replies[0]
        second_reply = await backend.send_message(handle, "next message")
        # second_reply is replies[1]
    """

    name = "fake"

    def __init__(self, replies: list[AgentReply], *, delay: float = 0.0) -> None:
        """replies: scripted sequence. Each start_session and send_message pops the next.

        ``delay``: seconds to ``asyncio.sleep`` before returning each reply — lets a test
        simulate a slow/hanging agent call (e.g. to exercise a caller's overall wall-clock
        timeout, such as U11's per-stage budget in jsa/pipeline/stages.py::run_stage)
        without any real network I/O. 0.0 (default) preserves the old instant-return
        behavior for all existing tests.
        """
        self._replies: list[AgentReply] = list(replies)
        self._delay = delay

    def _pop_reply(self) -> AgentReply:
        if not self._replies:
            raise IndexError("FakeAgentBackend: no more scripted replies")
        return self._replies.pop(0)

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        """Consume the first reply and return (handle, reply)."""
        if self._delay:
            await asyncio.sleep(self._delay)
        handle = FakeSessionHandle(id=str(uuid4()), external_id=None)
        reply = self._pop_reply()
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> FakeSessionHandle:
        """Return a new handle without consuming a reply (no-op resume)."""
        return FakeSessionHandle(id=str(uuid4()), external_id=external_id)

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Consume and return the next scripted reply."""
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._pop_reply()

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op."""


class CapturingBackend(FakeAgentBackend):
    """FakeAgentBackend that also records the initial_user_msg of a fresh session, so a
    test can assert what was actually injected into the prompt (e.g. a base-CV skeleton,
    or the absence thereof)."""

    def __init__(self, replies: list[AgentReply]) -> None:
        super().__init__(replies)
        self.captured_initial_msg: str | None = None

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        self.captured_initial_msg = initial_user_msg
        return await super().start_session(system_prompt, initial_user_msg)
