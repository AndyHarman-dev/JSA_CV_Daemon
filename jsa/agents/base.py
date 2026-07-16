"""AgentBackend ABC, SessionHandle, AgentReply, and HistoryTurn dataclasses."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


class AgentTimeout(Exception):
    """Raised when a backend times out waiting for the sentinel from the agent."""


class AgentLimitReached(RuntimeError):
    """Raised when a backend hits its usage/rate limit.

    The raw output snippet from the backend is passed as the message so callers
    can log it for diagnosis.
    """


class AgentOutputTruncated(RuntimeError):
    """Raised when a backend's response was truncated before completion.

    Typically caused by ``stop_reason == "max_tokens"``: the response hit the
    output token cap before the model finished, so the raw text is not a
    complete sentinel-terminated reply. Retrying the same request unchanged
    will truncate again — the fix is to raise ``max_tokens`` (see
    ``Settings.max_tokens`` / ``JSA_MAX_TOKENS``) or shorten the input.
    """


class AgentRequestError(RuntimeError):
    """Raised for non-retryable request/response problems.

    Covers two cases: the backend rejected the request as malformed (e.g.
    ``anthropic.BadRequestError``) — retrying the identical request will fail
    the same way — and a response whose shape can't be parsed into a reply
    (an empty content list, or a content block with no text).
    """


@dataclass(frozen=True)
class AgentReply:
    raw: str                                    # full text returned by the model
    content: str                                # text inside the sentinel block
    kind: Literal["final", "needs_input"]
    question: str | None = None                 # populated iff kind == "needs_input"


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


class AgentBackend(ABC):
    """Convention (not enforced by this ABC): if an implementation spawns a
    subprocess, it MUST be killable — e.g. via jsa.agents._subprocess.run_killable
    — so Orchestrator.cancel_task() can actually stop it. A subprocess run via
    plain blocking subprocess.run()-in-a-thread cannot be interrupted by
    task.cancel() and will burn API quota to completion regardless of
    cancellation. See jsa/agents/_subprocess.py's module docstring."""

    name: str                                   # "claude-cli" | "google-cli" | "anthropic"

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
