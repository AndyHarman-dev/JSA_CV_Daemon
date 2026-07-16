"""FakeAgentBackend — scripted, deterministic AgentBackend for testing."""

from dataclasses import dataclass
from uuid import uuid4

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, HistoryTurn, SessionHandle


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
    # Default: behaves like a CLI backend (no history-only replay). Pass
    # supports_history_replay=True at construction (or subclass and override
    # the class attribute) to simulate an Anthropic-style history-capable
    # backend for backend-switch-retention tests.
    supports_history_replay = False

    def __init__(
        self,
        replies: list[AgentReply],
        *,
        raise_limit_times: int = 0,
        supports_history_replay: bool | None = None,
    ) -> None:
        """replies: scripted sequence. Each start_session and send_message pops the next.

        raise_limit_times: if > 0, the first N calls to start_session/send_message
        (whichever is invoked next) raise AgentLimitReached instead of popping a
        reply, simulating a backend that hits its rate limit N times before
        succeeding — for backoff/retry tests.

        supports_history_replay: overrides the class-level default for this
        instance, so a single test can construct both a "history-capable" fake
        (like AnthropicAPIBackend) and a "CLI-style" fake (like ClaudeCliBackend)
        without separate subclasses.
        """
        self._replies: list[AgentReply] = list(replies)
        self._raise_limit_times = raise_limit_times
        self._limit_raises_done = 0
        if supports_history_replay is not None:
            self.supports_history_replay = supports_history_replay

    def _pop_reply(self) -> AgentReply:
        if not self._replies:
            raise IndexError("FakeAgentBackend: no more scripted replies")
        return self._replies.pop(0)

    def _maybe_raise_limit(self) -> None:
        if self._limit_raises_done < self._raise_limit_times:
            self._limit_raises_done += 1
            raise AgentLimitReached(
                f"Simulated limit hit ({self._limit_raises_done}/{self._raise_limit_times})"
            )

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        """Consume the first reply and return (handle, reply)."""
        self._maybe_raise_limit()
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
        self._maybe_raise_limit()
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
