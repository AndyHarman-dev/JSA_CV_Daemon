"""AgentBackend ABC, SessionHandle, AgentReply, and HistoryTurn dataclasses."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, Literal


class AgentTimeout(Exception):
    """Raised when a backend times out waiting for the sentinel from the agent."""


class AgentLimitReached(RuntimeError):
    """Raised when a backend hits its usage/rate limit.

    The raw output snippet from the backend is passed as the message so callers
    can log it for diagnosis.
    """


class AgentBackendUnavailable(RuntimeError):
    """Raised when a backend cannot serve a request for a reason that retrying
    the SAME backend is unlikely to fix, but that is distinct from a quota/rate
    signal (AgentLimitReached) or a pure timeout (AgentTimeout).

    Covers: bad model/config, auth errors, and transient overload/gateway
    failures (e.g. a flaky free-tier backend's intermittent 5xx responses or
    null-content replies) that have already exhausted their in-backend retry
    budget. Like AgentLimitReached and AgentTimeout, this is meant to engage
    BF-19's backend-fallback chain rather than hard-failing the job.
    """


@dataclass(frozen=True)
class AgentReply:
    raw: str                                    # full text returned by the model
    content: str                                # text inside the sentinel block
    kind: Literal["final", "needs_input"]
    question: str | None = None                 # populated iff kind == "needs_input"
    suggested_replies: list[str] | None = None   # optional, only iff kind == "needs_input"


@dataclass
class SessionHandle:
    """Opaque per-backend handle. Backends may attach process/connection state.
    Backends MUST persist `external_id` (e.g., a Claude CLI session id) so the
    handle can be reconstructed by `restore_session` after a tear-down."""
    id: str
    external_id: str | None = None  # backend-specific resume token, persisted on Job


@dataclass(frozen=True)
class HistoryTurn:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class AgentChunk:
    """One streamed delta, already classified. ``content`` is visible model
    output; ``reasoning`` is a genuine separate channel (e.g. claude-cli's
    thinking_delta) — never synthesized when no such channel exists."""
    kind: Literal["content", "reasoning"]
    text: str


OnChunk = Callable[[AgentChunk], Awaitable[None]]

# Called by a backend right before it replays a whole turn on the SAME logical
# request (a sentinel-nudge retry, or an in-backend transient-HTTP retry) —
# never at true turn completion, which is exclusively stages.py's
# ChunkAccumulator.end_turn() call. A backend that declares supports_streaming
# MUST accept an optional ``on_retry: OnRetry | None = None`` keyword on
# start_session/send_message alongside on_chunk, even if (like
# AnthropicAPIBackend, which has no in-flight replay-and-retry shape of its
# own) it never actually calls it — see jsa/pipeline/streaming.py's
# ChunkAccumulator.end_turn for what the bound callback actually does
# (force-flush + publish AgentTurnEndEvent(superseded=True)).
OnRetry = Callable[[], Awaitable[None]]


class AgentBackend(ABC):
    """Convention (not enforced by this ABC): if an implementation spawns a
    subprocess, it MUST be killable — e.g. via jsa.agents._subprocess.run_killable
    — so Orchestrator.cancel_task() can actually stop it. A subprocess run via
    plain blocking subprocess.run()-in-a-thread cannot be interrupted by
    task.cancel() and will burn API quota to completion regardless of
    cancellation. See jsa/agents/_subprocess.py's module docstring."""

    name: str                                   # "claude-cli" | "google-cli" | "anthropic" | "opencode-zen"

    # True only for backends whose wire protocol can enforce a JSON schema on the
    # model's reply (Anthropic forced tool-use, OpenCode Zen's response_format). CLI
    # backends have no such channel and stay on the sentinel grammar unconditionally.
    # Hard-coded per backend (not runtime-detected) — see the structured-output plan's
    # "Locked decisions" #1.
    #
    # Contract: a backend that sets this True MUST accept an optional
    # ``structured_schema: dict | None = None`` keyword on ``start_session`` and
    # ``restore_session`` (anthropic, opencode-zen; also any structured-capable test
    # fake — see tests/backend/fakes/fake_backend.py). ``jsa/pipeline/stages.py``
    # passes that kwarg ONLY when it has a non-None schema for the backend in hand, so
    # a backend that leaves this False (every CLI backend) is never asked to accept
    # it — do not add an unused accept-and-ignore parameter to a backend that stays
    # False; there is no call site that would ever supply it.
    supports_structured_output: ClassVar[bool] = False

    # True only for backends with a genuine token-level channel (an SSE stream, or
    # claude-cli's --output-format stream-json). Hard-coded per backend, never
    # runtime-detected — mirrors supports_structured_output above. Contract: a
    # backend that sets this True MUST accept an optional
    # ``on_chunk: OnChunk | None = None`` keyword on ``start_session`` and
    # ``send_message`` (restore_session never generates new assistant turns, so it
    # never streams). jsa/pipeline/stages.py passes it ONLY when it has a callback
    # in hand for a backend that supports it — a backend left False (e.g.
    # google-cli, whose agy CLI has no streaming flag) is never asked to accept
    # one. Streaming is best-effort: any failure inside a backend's on_chunk path
    # must be swallowed and the synchronous AgentReply returned intact.
    supports_streaming: ClassVar[bool] = False

    @abstractmethod
    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[SessionHandle, AgentReply]:
        """Open a fresh session. Returns the handle and the agent's first reply."""

    @abstractmethod
    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> SessionHandle:
        """Reconstruct a previously-ended session WITHOUT generating new assistant turns."""

    @abstractmethod
    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply: ...

    @abstractmethod
    async def end_session(self, handle: SessionHandle) -> None: ...
