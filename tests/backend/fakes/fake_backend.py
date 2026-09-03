"""FakeAgentBackend — scripted, deterministic AgentBackend for testing."""

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from jsa.agents.base import AgentBackend, AgentChunk, AgentReply, HistoryTurn, OnChunk, SessionHandle


@dataclass
class FakeSessionHandle(SessionHandle):
    """Concrete SessionHandle for FakeAgentBackend.

    ``structured_schema``/``structured_enabled`` mirror AnthropicSessionHandle /
    OpenCodeZenSessionHandle's shape closely enough for _log_session_mode
    (jsa/pipeline/stages.py) to read a meaningful mode off this fake too.
    """

    # id and external_id are inherited from SessionHandle
    structured_schema: dict[str, Any] | None = None
    structured_enabled: bool = True


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

    ``supports_structured_output`` (default ``False``, matching every CLI backend and
    every test written before the structured-output plan) is an instance attribute
    that shadows the ``AgentBackend`` ClassVar of the same name — pass
    ``supports_structured_output=True`` to simulate an Anthropic/OpenCode-Zen-shaped
    structured-capable backend without a dedicated subclass. All three session methods
    accept-and-record a ``structured_schema`` kwarg regardless of this flag, since a
    structured-capable fake is on the ENFORCING side of the plan's "a backend that sets
    supports_structured_output=True must accept structured_schema on start_session/
    restore_session" contract (see jsa/agents/base.py), not the CLI ignoring side.
    """

    name = "fake"

    def __init__(
        self,
        replies: list[AgentReply],
        *,
        supports_structured_output: bool = False,
        supports_streaming: bool = False,
        scripted_chunks: list[list[AgentChunk]] | None = None,
    ) -> None:
        """replies: scripted sequence. Each start_session and send_message pops the next.

        ``scripted_chunks``, when given, is a parallel list of AgentChunk lists —
        one entry per start_session/send_message call — replayed through
        ``on_chunk`` (if the caller supplied one) before the corresponding reply
        is returned.
        """
        self._replies: list[AgentReply] = list(replies)
        self.supports_structured_output = supports_structured_output
        self.supports_streaming = supports_streaming
        self._scripted_chunks: list[list[AgentChunk]] = list(scripted_chunks or [])
        # Recorded for structured-mode tests: the parity gate asserts the schema kwarg
        # actually arrived, and a fresh-session-retry test asserts start_session was
        # re-issued the expected number of times with identical args.
        self.received_schemas: list[dict[str, Any] | None] = []
        self.start_session_call_count = 0

    def _pop_reply(self) -> AgentReply:
        if not self._replies:
            raise IndexError("FakeAgentBackend: no more scripted replies")
        return self._replies.pop(0)

    async def _emit_scripted_chunks(self, on_chunk: OnChunk | None) -> None:
        if on_chunk is None or not self._scripted_chunks:
            return
        for chunk in self._scripted_chunks.pop(0):
            await on_chunk(chunk)

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        """Consume the first reply and return (handle, reply)."""
        self.start_session_call_count += 1
        self.received_schemas.append(structured_schema)
        await self._emit_scripted_chunks(on_chunk)
        handle = FakeSessionHandle(
            id=str(uuid4()),
            external_id=None,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
        )
        reply = self._pop_reply()
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
    ) -> FakeSessionHandle:
        """Return a new handle without consuming a reply (no-op resume)."""
        self.received_schemas.append(structured_schema)
        return FakeSessionHandle(
            id=str(uuid4()),
            external_id=external_id,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
        )

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentReply:
        """Consume and return the next scripted reply."""
        await self._emit_scripted_chunks(on_chunk)
        return self._pop_reply()

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op."""


class CapturingBackend(FakeAgentBackend):
    """FakeAgentBackend that also records the initial_user_msg of a fresh session, so a
    test can assert what was actually injected into the prompt (e.g. a base-CV skeleton,
    or the absence thereof)."""

    def __init__(self, replies: list[AgentReply], **kwargs: Any) -> None:
        super().__init__(replies, **kwargs)
        self.captured_initial_msg: str | None = None

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        self.captured_initial_msg = initial_user_msg
        return await super().start_session(system_prompt, initial_user_msg, structured_schema)
