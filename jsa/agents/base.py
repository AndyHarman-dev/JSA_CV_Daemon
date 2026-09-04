"""AgentBackend ABC, SessionHandle, AgentReply, and HistoryTurn dataclasses."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Literal


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


class ToolsUnsupported(Exception):
    """Raised when a specific tool-mode request cannot be served on the NATIVE
    rung, so ``jsa/pipeline/tool_loop.py``'s ladder should downgrade this turn to
    the prompt rung and retry once.

    **Deliberately NOT a subclass of ``AgentBackendUnavailable``, and every
    per-backend ``_ToolsRejected`` subclasses THIS, not that.** Each backend that
    speaks native tools defines its own module-private
    ``_ToolsRejected(ToolsUnsupported)`` — ``_openai_compat.py`` (canonical, inherited
    by ``mistral``/``openrouter``/``opencode-go``/``gemini``), ``anthropic_api.py``,
    and ``opencode_zen.py`` (its own independent copy, per CLAUDE.md's no-shared-base
    rule for that module). They are raised from a permanent 4xx **only when native
    tool definitions were actually in the payload**, and they propagate out of the
    backend untouched — there is no in-backend degrade-and-retry for tools, unlike
    ``_CacheRejected``/``_ReasoningRejected``.

    Two reasons this exception sits outside the ``AgentBackendUnavailable`` hierarchy,
    and neither is negotiable:

    1. **The fallback is owned by a different layer.** Caching and reasoning are
       optional enrichments with no functional substitute, so they shed *in-backend*
       and retry once clean. Tool mode's fallback is the rung ladder in
       ``jsa/pipeline/tool_loop.py`` (native → prompt → rewrite), which lives above the
       backend. A clean in-backend retry here would re-send a system prompt that
       explains a tool contract with no tools attached — incoherent.
    2. **It must never cost the job a BF-19 slot.** ``Orchestrator._run_one`` routes
       ``AgentLimitReached``/``AgentTimeout``/``AgentBackendUnavailable`` into
       ``_advance_backend_or_fail``. If a tools-only degrade were an
       ``AgentBackendUnavailable``, an escaped instance would advance the *whole job*
       to the next configured backend — exactly the loss the rung ladder exists to
       prevent.

    Consequences, all required: ``_call_api``'s bounded degrade loop must NOT grow a
    branch for it (it propagates, by design); ``tool_loop.py`` must catch
    ``ToolsUnsupported`` around BOTH its ``restore_session`` entry and its
    ``send_tool_results`` follow-ups (an unguarded ``send_tool_results`` lets a
    mid-turn rejection escape into ``_run_one``'s generic ``except Exception`` and
    hard-fail the job with no BF-19 and no rung 3); and a genuine auth error or
    bad-model 4xx must keep raising ``AgentBackendUnavailable``
    (or ``AgentLimitReached``/``AgentTimeout``) so BF-19 still engages for those.
    """


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by the model, already normalized to a single
    shape regardless of wire origin (Anthropic ``tool_use`` block, OpenAI-compatible
    ``tool_calls`` entry, Gemini ``functionCall`` part, or a parsed
    ``<<<TOOL_CALLS>>>`` prompt-rung block — see ``jsa/agents/protocol.py``).

    ``id`` is synthesized (``call_0``, ``call_1``, ...) by whichever layer parses the
    reply when the wire has no natural call id (e.g. the prompt rung); backends with a
    real provider-issued id may use that instead. ``arguments`` is always a plain
    JSON-object dict — never a raw string awaiting a second ``json.loads``.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The outcome of executing one ``ToolCall``, ready to send back to the model via
    ``AgentBackend.send_tool_results``. ``content`` is the JSON-serializable result
    dict produced by the applier (``jsa/schema/patch.py``) or synthesized by the loop
    itself (``not_executed``/``budget_exhausted`` — see ``jsa/pipeline/tool_loop.py``)."""

    call_id: str
    name: str
    ok: bool
    content: Any


@dataclass(frozen=True)
class AgentReply:
    raw: str                                    # full text returned by the model
    content: str                                # text inside the sentinel block
    kind: Literal["final", "needs_input", "tool_calls"]
    question: str | None = None                 # populated iff kind == "needs_input"
    suggested_replies: list[str] | None = None   # optional, only iff kind == "needs_input"
    tool_calls: list[ToolCall] | None = None     # populated iff kind == "tool_calls"


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

    # True only for backends whose wire protocol has a genuine tool/function-calling
    # channel (Anthropic forced tool-use, an OpenAI-compatible `tools` + `tool_choice`,
    # Gemini `functionDeclarations`). Hard-coded per backend, never runtime-detected —
    # mirrors supports_structured_output above, with the SAME OpenCodeGoBackend
    # exception: its `/messages` protocol instances set this as an INSTANCE attribute
    # in __init__ (forced tool-use does not take on that gateway path), so callers must
    # read it off the instance, never the class — see CLAUDE.md's structured-output
    # section for the identical `OpenCodeGoBackend` reasoning applied to this flag.
    #
    # Contract: a backend that sets this True MUST (a) accept an optional
    # ``tools: tuple[ToolSpec, ...] | None = None`` keyword on ``start_session`` and
    # ``restore_session`` (jsa/agents/tool_spec.py defines ToolSpec and the
    # provider-shape renderers each backend converts these into internally), and
    # (b) override ``send_tool_results`` below. ``jsa/pipeline/tool_loop.py`` passes
    # ``tools=`` ONLY when it has a non-None tuple for the backend in hand, so a
    # backend that leaves this False is never asked to accept it — do not add an
    # unused accept-and-ignore parameter to a backend that stays False.
    supports_native_tools: ClassVar[bool] = False

    # True when ``restore_session`` actually APPLIES the ``system_prompt`` it is handed
    # to the restored session. False for a backend whose session lives on the provider's
    # side and is resumed by id (``claude-cli``'s ``--resume <uuid>``, ``google-cli``'s
    # ``--conversation <uuid>``): those implementations deliberately ignore both
    # ``system_prompt`` and ``history`` — the CLI already holds the conversation, and no
    # ``--system-prompt`` flag is passed on a resume, so a NEW system prompt handed to
    # ``restore_session`` is silently dropped on the floor.
    #
    # This is the load-bearing precondition for jsa/pipeline/tool_loop.py's PROMPT rung
    # (rung 2), whose entire transport is a system prompt carrying the tool contract
    # (``jsa/pipeline/prompt_assembly.py::_tool_contract``). On a False backend that rung
    # cannot work by construction: the model never sees the contract, answers an ordinary
    # ``<<<FINAL>>>``, the loop discards it as unparseable, and rung 3 re-sends the same
    # instruction into a provider-held conversation that now contains the instruction
    # twice. So ``jsa/pipeline/stages.py::_tools_for`` skips the tool loop outright for a
    # backend that is neither ``supports_native_tools`` nor this — do not "simplify" that
    # gate away, and do not flip this True for a resume-by-id CLI backend without first
    # giving its ``send_message`` a real system-prompt channel.
    restore_applies_system_prompt: ClassVar[bool] = True

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

    async def send_tool_results(
        self, handle: SessionHandle, results: list[ToolResult]
    ) -> AgentReply:
        """Continue a native tool-mode session with the outcomes of the last round of
        tool calls. NOT a fifth abstract method — same conditional-capability rule as
        ``structured_schema``/``on_chunk`` above (see ``supports_native_tools``'s
        docstring): only a backend that sets ``supports_native_tools = True`` is ever
        called through here, and such a backend MUST override this. The default raises
        so a backend that forgets to override it fails loudly instead of silently
        no-opping.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support native tool calling "
            "(supports_native_tools is False)"
        )

    @abstractmethod
    async def end_session(self, handle: SessionHandle) -> None: ...
