"""Shared machinery for OpenAI-compatible chat-completions HTTP backends.

Extracted from ``jsa/agents/opencode_zen.py`` (the reference HTTP backend) as part of
the multi-backend-model-select plan's Phase 2. Deliberately NOT wired back into
``opencode_zen.py`` — refactoring that module onto this base is optional and gated on
its 935-line test file (``tests/backend/test_opencode_zen.py``) passing unchanged
afterwards; that refactor was not attempted here, so ``opencode_zen.py`` keeps its own
independent (behaviourally identical) copy of this logic. Accept the duplication.

``OpenAICompatBackend`` implements everything an OpenAI-compatible chat/completions
backend needs — payload build, ``response_format`` with ``"strict": false`` (see the
module docstring reasoning in ``opencode_zen.py`` — the turn models' nested ``$defs``
are not recursively strict), the three-way error classification (quota/rate → immediate
``AgentLimitReached``; transient overload/gateway → retried in-process then
``AgentBackendUnavailable``; permanent 4xx → immediate ``AgentBackendUnavailable``), the
per-session structured→sentinel downgrade, the sentinel-compliance nudge, and native
tool calling (``tools`` + ``tool_choice: "required"``, with ``_ToolsRejected`` handing
the rung decision back to ``jsa/pipeline/tool_loop.py`` rather than degrading here).
Concrete subclasses (``MistralBackend``, ``OpenRouterBackend``, and the
``/chat/completions`` half of ``OpenCodeGoBackend``) need only set ``name``,
``endpoint_url``, ``env_vars``, and ``default_model`` as class attributes; ``_extra_payload`` is an optional hook for a
subclass-specific top-level payload key (OpenRouter's routing guard).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar, TypeVar

import httpx

from jsa.agents.base import (
    AgentBackend,
    AgentBackendUnavailable,
    AgentChunk,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    HistoryTurn,
    OnChunk,
    OnRetry,
    SessionHandle,
    ToolCall,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.protocol import ProtocolError, parse_reply
from jsa.agents.tool_spec import ToolSpec, to_openai_tools
from jsa.schema.turn_models import parse_structured_reply_for_schema

logger = logging.getLogger(__name__)

# Fixed response_format schema name — mirrors AnthropicAPIBackend's fixed tool name
# ("respond"): the backend layer is stage-agnostic, so there is no per-stage name to
# use here either.
_RESPONSE_FORMAT_NAME = "structured_reply"

# See opencode_zen.py's module-level comment for why these are retried in-process
# rather than immediately switching backends: a transient overload/gateway failure
# often succeeds on the very next call to the SAME backend, so retrying here is
# cheaper than burning a BF-19 backend switch on a blip. NOTE for test authors:
# ``retry_transient``'s ``backoff`` parameter binds ``_RETRY_BACKOFF_SECONDS`` at
# def time (default-argument evaluation), so monkeypatching this module-level
# constant after import has NO effect on an already-defined call — to disable
# sleeps in a test, monkeypatch ``jsa.agents._openai_compat.asyncio.sleep``
# instead (see this repo's test fixtures for the pattern).
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 3.0)  # sleep before attempt 2 and attempt 3


class TransientBackendError(Exception):
    """Internal only: signals a single attempt's retryable overload/gateway failure
    to ``retry_transient``. Never escapes this module (or any module that reuses
    ``retry_transient``) — an exhausted retry budget is converted to
    ``AgentBackendUnavailable``. Shared (not per-subclass) so OpenCode-GO's
    ``/messages`` protocol path (which does not subclass this machinery — see
    ``opencode_go.py``) can reuse the same retry loop instead of duplicating it.
    """


class _CacheRejected(AgentBackendUnavailable):
    """Internal only: a permanent 4xx from ``_call_api_once`` while cache_control
    fields were actually present in the payload (i.e. ``_system_content`` returned
    a block-array shape, not the plain string), raised instead of plain
    ``AgentBackendUnavailable`` so ``_call_api`` can catch it, disable prompt
    caching for the rest of this backend instance's life, and retry the same call
    once clean rather than dropping the backend out of BF-19 over a caching-only
    routing rejection. See ``_system_content``'s docstring (CLAUDE.md -> "Prompt
    caching" -> OpenRouter's degrade-on-4xx). Subclasses ``AgentBackendUnavailable``
    as a safety net: an instance that somehow escapes the catch still classifies
    correctly for BF-19."""


class _ReasoningRejected(AgentBackendUnavailable):
    """Internal only: a permanent 4xx from ``_call_api_once`` while a reasoning
    opt-in field was actually present in the payload (i.e. ``_reasoning_payload``
    returned a non-empty dict), raised instead of plain
    ``AgentBackendUnavailable`` so ``_call_api`` can catch it, disable the
    reasoning opt-in for the rest of this backend instance's life, and retry the
    same call once clean.

    Exactly the ``_CacheRejected`` shape and for exactly the same reason: asking
    for reasoning is an OPTIONAL enrichment, so a provider that rejects the field
    (an upstream that does not support it, or — on OpenRouter — a
    ``provider.require_parameters`` guard that filters out every eligible
    provider because of it) must cost this backend its thinking stream, never its
    BF-19 slot. Subclasses ``AgentBackendUnavailable`` as a safety net."""


class _ToolsRejected(ToolsUnsupported):
    """Internal only: a permanent 4xx from ``_call_api_once`` while native tool
    definitions were actually present in the payload (``tools`` + ``tool_choice``).

    **Subclasses ``ToolsUnsupported``, NOT ``AgentBackendUnavailable``** — this is the
    one degrade in this module that deliberately breaks the ``_CacheRejected`` /
    ``_ReasoningRejected`` mould, and it must not be "fixed" back into line with them.
    The revision-tool-use plan's Phase 3 preamble sketches the ``_CacheRejected`` mirror
    (subclass ``AgentBackendUnavailable``, catch it here, retry once clean); that is the
    wrong analogy, for two independent reasons:

    1. Caching and reasoning are optional enrichments with NO functional fallback, so
       they must shed in-backend and retry clean here. Tool mode DOES have a fallback,
       and it is owned by a different layer entirely — ``jsa/pipeline/tool_loop.py``'s
       native -> prompt rung ladder, which re-establishes the session with a
       tool-contract system prompt and no wire tools at all. Retrying clean in-process
       would instead send a tool-contract system prompt with no tools attached and hand
       the resulting sentinel/structured reply back to the loop, which is semantically
       incoherent.
    2. If this subclassed ``AgentBackendUnavailable``, an escaped instance would tell
       BF-19 to advance the whole job to the next configured backend over a tools-only
       degrade — exactly the loss the rung ladder exists to prevent. See
       ``ToolsUnsupported``'s docstring in ``jsa/agents/base.py``.

    Consequences, both required: ``_call_api``'s ``for _ in range(2)`` degrade loop must
    NOT grow a third ``except`` branch for this (it propagates, by design), and
    ``tool_loop.py`` must catch ``ToolsUnsupported`` around BOTH its ``restore_session``
    entry and its ``send_tool_results`` follow-ups.
    """


_NUDGE_TEXT = (
    "Your previous response was missing the required sentinel block. "
    "Please restate your response and end it with exactly one of:\n"
    "<<<NEED_INPUT>>>\n<your question>\n<<<END>>>\n"
    "or\n"
    "<<<FINAL>>>\n<your final content>\n<<<END>>>"
)

# Mode-conditional variant used ONLY the turn a structured-mode session downgrades
# (see _parse_structured_with_downgrade): the model was told a JSON-schema contract
# applies this session, so the nudge must explicitly say that contract no longer
# holds before restating the sentinel grammar.
_DOWNGRADE_NUDGE_TEXT = (
    "Disregard the structured-output/JSON-schema contract from earlier in this "
    "session — it no longer applies. Restate your previous response using the "
    "sentinel-block format instead, and end it with exactly one of:\n"
    "<<<NEED_INPUT>>>\n<your question>\n<<<END>>>\n"
    "or\n"
    "<<<FINAL>>>\n<your final content>\n<<<END>>>"
)


@dataclass(kw_only=True)
class OpenAICompatSessionHandle(SessionHandle):
    """Session handle shared by every OpenAI-compatible backend built on this base."""

    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can rebuild the messages array
    messages: list[dict] = field(default_factory=list)  # growing conversation list (no system entry)
    # The schema established at start/restore time (None = this session never
    # attempted structured mode).
    structured_schema: dict[str, Any] | None = None
    # Per-session downgrade flag (NEVER persisted — a restart re-attempts structured).
    # Only meaningful when structured_schema is not None; flips permanently to False
    # the first time a structured reply is unparseable.
    structured_enabled: bool = False
    # The native tool vocabulary established at start/restore time (None = this is not
    # a tool-mode session). Carried on the handle exactly the way ``structured_schema``
    # is, so ``send_message``/``send_tool_results`` can resolve it without the caller
    # (``jsa/pipeline/tool_loop.py``) having to re-supply it on every turn — it calls
    # ``restore_session(..., tools=specs)`` and then plain ``send_message(handle, text)``.
    tools: tuple[ToolSpec, ...] | None = None
    # The RAW provider ``tool_calls`` array from the most recent assistant reply, kept
    # verbatim so ``send_tool_results`` can replay it as the assistant turn that the
    # ``role="tool"`` result messages must answer. Deliberately NOT appended to
    # ``messages`` when the reply arrives: a turn that ends on a terminal tool
    # (finalize/ask_user) never sends results back, and an assistant ``tool_calls``
    # turn with no matching tool results is an invalid conversation on this wire shape.
    pending_tool_calls: list[dict] | None = None


def _flatten_text(value: Any) -> str:
    """Best-effort text out of a provider's nested chunk shape (a bare string, a
    list of ``{"type": ..., "text"/"summary"/"thinking": ...}`` objects, or one such
    object). Never raises on an unexpected shape — an unrecognized node contributes
    nothing rather than blowing up a request that otherwise succeeded."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "summary", "thinking"):
            if key in value:
                return _flatten_text(value[key])
    return ""


def _split_content_delta(value: Any) -> tuple[str, str]:
    """Split an OpenAI-compatible ``content`` field into ``(content, reasoning)``.

    Normally ``content`` is a plain string and this is a passthrough. Mistral is the
    exception: with ``reasoning_effort`` set, ``content`` becomes a LIST of chunks
    mixing ``{"type": "thinking", "thinking": [...]}`` with ``{"type": "text",
    "text": ...}`` (confirmed against Mistral's reasoning docs). Without this split
    the list would be appended straight into ``content_parts`` and the subsequent
    ``"".join`` would raise ``TypeError`` — so this is a type guard as much as a
    feature."""
    if isinstance(value, str):
        return value, ""
    if not isinstance(value, list):
        return "", ""
    content: list[str] = []
    reasoning: list[str] = []
    for chunk in value:
        if isinstance(chunk, str):
            content.append(chunk)
        elif isinstance(chunk, dict):
            if chunk.get("type") == "thinking":
                reasoning.append(_flatten_text(chunk.get("thinking")))
            else:
                content.append(_flatten_text(chunk.get("text")))
    return "".join(content), "".join(reasoning)


def _reasoning_delta_text(delta: dict) -> str:
    """Reasoning text out of one SSE ``delta``, across the three field conventions
    this wire shape has in the wild:

    * ``reasoning_content`` — the DeepSeek/OpenAI-compat convention, what
      ``opencode-go``'s routed models emit (the one that already worked);
    * ``reasoning`` — OpenRouter's legacy plain-string field;
    * ``reasoning_details`` — OpenRouter's current array-of-objects field.

    OpenRouter emits ``reasoning`` and ``reasoning_details`` together for the same
    tokens, so the first non-empty wins rather than concatenating and doubling the
    text."""
    for key in ("reasoning_content", "reasoning"):
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return _flatten_text(delta.get("reasoning_details"))


def _reasoning_only(on_chunk: OnChunk | None) -> OnChunk | None:
    """Wrap ``on_chunk`` so only ``kind="reasoning"`` chunks pass through — used
    for structured-schema calls, where the ``content`` delta is raw partial JSON
    (not useful to render as chat text) but a ``reasoning_content`` delta, when a
    routed model exposes one, still is. ``None`` in, ``None`` out."""
    if on_chunk is None:
        return None

    async def _filtered(chunk: AgentChunk, _cb: OnChunk = on_chunk) -> None:
        if chunk.kind == "reasoning":
            await _cb(chunk)

    return _filtered


def _parse_tool_calls(raw_calls: list[dict], backend_name: str) -> list[ToolCall]:
    """Normalize an OpenAI-compatible ``message.tool_calls`` array into ``ToolCall``s.

    ``function.arguments`` arrives on this wire shape as a JSON **string**, so it is
    ``json.loads``ed here — ``ToolCall.arguments`` is contractually always a plain
    dict, never a string awaiting a second parse (see ``jsa/agents/base.py``). A
    provider-issued ``id`` is preferred; ``call_{i}`` is synthesized only when the
    entry carries none, matching ``protocol.py``'s prompt-rung ids.

    Malformed input raises ``ProtocolError`` — the same strictness (and the same
    exception type) as ``protocol.py::_parse_tool_calls_block``: a half-readable tool
    batch must fail loudly, never execute partially. ``ProtocolError`` is deliberately
    NOT a ``TransientBackendError``, so a malformed batch is never retried as if it
    were a gateway blip.
    """
    calls: list[ToolCall] = []
    for i, entry in enumerate(raw_calls):
        if not isinstance(entry, dict):
            raise ProtocolError(
                f"malformed {backend_name} tool_calls: item {i} is not an object"
            )
        function = entry.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(
                f"malformed {backend_name} tool_calls: item {i} has no function name"
            )
        arguments = function.get("arguments")
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            # A zero-parameter tool (get_cv/get_letter) legitimately serializes to
            # "" / "{}" / null depending on the provider.
            parsed: Any = {}
        elif isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except ValueError as exc:
                raise ProtocolError(
                    f"malformed {backend_name} tool_calls: item {i} ({name}) has "
                    f"unparseable arguments JSON ({exc})"
                ) from None
        else:
            # Some gateways hand back an already-decoded object rather than a string.
            parsed = arguments
        if not isinstance(parsed, dict):
            raise ProtocolError(
                f"malformed {backend_name} tool_calls: item {i} ({name}) arguments "
                "are not a JSON object"
            )
        call_id = entry.get("id")
        calls.append(
            ToolCall(
                id=str(call_id) if call_id else f"call_{i}",
                name=name,
                arguments=parsed,
            )
        )
    if not calls:
        raise ProtocolError(f"malformed {backend_name} tool_calls: empty array")
    return calls


def _tool_calls_reply(calls: list[ToolCall]) -> AgentReply:
    """Wrap parsed ``ToolCall``s in an ``AgentReply``.

    ``raw``/``content`` carry the same canonical ``[{"name", "arguments"}, ...]`` JSON
    that ``protocol.py``'s ``<<<TOOL_CALLS>>>`` prompt-rung body uses, NOT the provider
    envelope — so the native and prompt rungs produce byte-comparable text for the same
    logical batch (the canonical-form invariant: the DB never stores provider wire
    format). Locked decision; do not switch this to the raw ``tool_calls`` array."""
    canonical = json.dumps([{"name": c.name, "arguments": c.arguments} for c in calls])
    return AgentReply(raw=canonical, content=canonical, kind="tool_calls", tool_calls=calls)


def _log_cached_tokens(backend_name: str, body: dict) -> None:
    """Log this response's cached-prompt-token count when the provider reported one.

    ``isinstance``-guarded at every level (not just a ``.get`` chain): an API that
    returns ``"usage": null`` or a wrong-typed field must not raise past a request
    that otherwise succeeded. Called once per successful response, for BOTH a text
    and a native tool-call reply."""
    usage = body.get("usage")
    prompt_tokens_details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    cached_tokens = (
        prompt_tokens_details.get("cached_tokens")
        if isinstance(prompt_tokens_details, dict)
        else None
    )
    if cached_tokens is not None:
        logger.info("%s API cached_tokens=%s", backend_name, cached_tokens)


def _text_reply_in_tool_mode(raw: str) -> AgentReply:
    """A text (non-``tool_calls``) reply received while native tools were attached.

    ``tool_choice: "required"`` is supposed to make this impossible, so this is the
    "model ignored the forcing" path. It must NOT go through ``_parse_with_nudge``:
    a tool session was never given the sentinel contract, so nudging for a
    ``<<<FINAL>>>`` block would be asking for something the system prompt never
    promised. ``parse_reply`` is still tried first — a model that answers a native
    tool session with a ``<<<TOOL_CALLS>>>``/``<<<FINAL>>>`` block is perfectly
    readable — and ONLY a "no sentinel block" failure degrades to an unparsed
    ``final``. Every other ``ProtocolError`` (notably a malformed ``TOOL_CALLS``
    body) propagates: converting that into a bogus ``kind="final"`` would hand
    ``stages.py`` raw prose to validate as a document.

    ``tool_loop.py`` treats any non-``tool_calls`` reply mid-loop as a broken tool
    turn and falls back to rung 3, which is the intended outcome here.
    """
    try:
        return parse_reply(raw)
    except ProtocolError as exc:
        if "no sentinel block" not in str(exc):
            raise
        return AgentReply(raw=raw, content=raw, kind="final")


def _active_schema(
    handle: OpenAICompatSessionHandle, explicit: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The schema actually in force for one call: None once the session has
    downgraded, regardless of what the caller (stages.py) keeps passing in."""
    if not handle.structured_enabled:
        return None
    return explicit if explicit is not None else handle.structured_schema


_RetryResult = TypeVar("_RetryResult")


async def retry_transient(
    call_once: Callable[[], Awaitable[_RetryResult]],
    *,
    unavailable_message: str,
    max_attempts: int = _MAX_ATTEMPTS,
    backoff: tuple[float, ...] = _RETRY_BACKOFF_SECONDS,
    on_retry: OnRetry | None = None,
) -> _RetryResult:
    """Call ``call_once()`` up to ``max_attempts`` times, retrying only on
    ``TransientBackendError`` (with a short sleep between attempts) and converting
    an exhausted budget into ``AgentBackendUnavailable``. Any other exception
    (``AgentLimitReached``, ``AgentTimeout``, a permanent-4xx
    ``AgentBackendUnavailable``) propagates immediately, unretried — see
    ``OpenAICompatBackend._call_api_once``'s docstring for why.

    ``on_retry``, when given, is awaited right before each retried attempt (i.e.
    once per loop iteration past the first) — this is the same-turn "streamed
    partials must be retractable" hook (see jsa/agents/base.py's ``OnRetry``
    docstring / the agent-chat-upgrade plan's Phase 6): if attempt 1 already
    streamed chunks before failing transiently, attempt 2 must not append onto
    the same buffer.

    Generic over ``call_once``'s return type purely so ``_call_api_once`` can widen
    to ``str | list[dict]`` (a text reply, or a raw native ``tool_calls`` array)
    without this function having to know about either. Zero runtime effect —
    ``_RetryResult`` binds per call site (``str`` for OpenCode-GO's ``/messages``
    protocol, which never carries tools).
    """
    last_exc: TransientBackendError | None = None
    for attempt in range(max_attempts):
        try:
            return await call_once()
        except TransientBackendError as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                logger.warning(
                    "%s (attempt %d/%d), retrying: %s",
                    unavailable_message, attempt + 1, max_attempts, exc,
                )
                if on_retry is not None:
                    await on_retry()
                await asyncio.sleep(backoff[attempt])
                continue
    raise AgentBackendUnavailable(f"{unavailable_message}: {last_exc}") from None


class OpenAICompatBackend(AgentBackend):
    """Base for OpenAI-compatible chat/completions HTTP backends.

    Subclasses set ``name``, ``endpoint_url``, ``env_vars`` (an ordered fallback
    chain of environment variable names for the API key — the first one set and
    non-empty wins), and ``default_model`` as class attributes. Everything else is
    inherited unchanged from this class.
    """

    supports_structured_output = True
    # Real SSE channel via `stream: true` on the chat-completions endpoint. Content
    # always; `reasoning_content` only when a routed model actually emits it (never
    # fabricated). Structured mode (response_format) is NOT streamed — a partial
    # JSON object is not useful to show the user turn-by-turn, and self-heal/wire-
    # retry parsing needs the complete body regardless — so streaming is only
    # attempted when structured_schema is None for a given call.
    supports_streaming = True
    # Native function calling via `tools` + `tool_choice: "required"` on this wire
    # shape. Inherited as True by MistralBackend and OpenRouterBackend;
    # OpenCodeGoBackend narrows it to an INSTANCE attribute (its `/messages` protocol
    # is not wired for tools) — see that module's __init__.
    supports_native_tools = True

    endpoint_url: ClassVar[str]
    env_vars: ClassVar[tuple[str, ...]]
    default_model: ClassVar[str]

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 180.0,
        prompt_caching: bool = True,
    ) -> None:
        self._model = model if model is not None else self.default_model
        self._timeout = timeout
        self._prompt_caching = prompt_caching
        # Reasoning opt-in, per-instance and never persisted — flipped off for the
        # rest of this instance's life by ``_call_api``'s ``_ReasoningRejected``
        # degrade. Inert for subclasses that don't override ``_reasoning_payload``.
        self._reasoning = True

    def _extra_payload(self, system_prompt: str) -> dict[str, Any]:
        """Hook for subclass-specific top-level payload keys. Default: none.
        OpenRouter overrides this to add its mandatory routing guard; Mistral
        overrides it to add a ``prompt_cache_key`` derived from ``system_prompt``."""
        return {}

    def _reasoning_payload(self) -> dict[str, Any]:
        """Hook for the top-level request keys that ask this provider to emit a
        reasoning/thinking stream. Default: none — a backend that doesn't override
        this sends a byte-identical payload to its pre-reasoning shape, and can
        never raise ``_ReasoningRejected``.

        Overrides MUST return ``{}`` when ``self._reasoning`` is False, so the
        degrade path below actually degrades. A non-empty return is what
        ``_call_api_once`` treats as "reasoning fields present" when classifying a
        permanent 4xx."""
        return {}

    def _system_content(self, system_prompt: str) -> str | list[dict]:
        """Hook for a subclass-specific system-message shape. Default: the bare
        string this backend has always sent — ``MistralBackend`` inherits this
        unchanged (its caching signal is a top-level ``_extra_payload`` field, not a
        system-content shape change). ``OpenRouterBackend`` overrides this to add an
        explicit ``cache_control`` breakpoint when prompt caching is enabled; a list
        return value is what ``_call_api_once`` treats as "cache fields present" for
        the ``_CacheRejected`` degrade path below."""
        return system_prompt

    def _api_key(self) -> str:
        for env_var in self.env_vars:
            value = os.environ.get(env_var)
            if value:
                return value
        return ""

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(
            system_prompt, messages, structured_schema, on_chunk, on_retry, tools
        )
        pending_tool_calls: list[dict] | None = None
        if isinstance(raw, list):
            reply = _tool_calls_reply(_parse_tool_calls(raw, self.name))
            pending_tool_calls = raw
            structured_enabled = False
            # The assistant tool_calls turn is NOT appended here — see
            # OpenAICompatSessionHandle.pending_tool_calls' docstring.
        elif tools:
            # Tool mode, but the model answered text despite tool_choice: "required".
            # Must NOT go through _parse_structured_with_downgrade: with no schema that
            # falls into _parse_with_nudge, which re-prompts for a <<<FINAL>>> block a
            # tool session was never given the contract for (and replays WITHOUT tools
            # attached). Same handling send_message's tool branch already had.
            reply = _text_reply_in_tool_mode(raw)
            structured_enabled = False
            messages.append({"role": "assistant", "content": reply.raw})
        else:
            reply, structured_enabled = await self._parse_structured_with_downgrade(
                system_prompt, messages, raw, structured_schema, on_chunk, on_retry
            )
            messages.append({"role": "assistant", "content": reply.raw})
        handle = OpenAICompatSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_enabled,
            tools=tools,
            pending_tool_calls=pending_tool_calls,
        )
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> OpenAICompatSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.

        ``tools``, when given, puts this session in native tool mode for the rest of
        its life: every subsequent ``send_message``/``send_tool_results`` POST carries
        the rendered ``tools`` + ``tool_choice: "required"`` and NO ``response_format``.
        This is the entry point ``jsa/pipeline/tool_loop.py::_enter_rung`` uses (a
        revision always restores, never starts fresh).
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        return OpenAICompatSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
            tools=tools,
        )

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> AgentReply:
        """Append a user turn, call the API, parse and store the assistant reply.

        Both turns are appended to handle.messages only after a successful API
        call, keeping the list coherent if the call times out or raises.

        The native tool vocabulary is resolved from the HANDLE (set by
        ``start_session``/``restore_session``), never passed in here — ``tool_loop.py``
        calls this as a plain ``send_message(handle, instruction)``, exactly as
        ``structured_schema`` is resolved from the handle when omitted.
        """
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        schema = _active_schema(handle, structured_schema)
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(
            handle.system_prompt, pending_messages, schema, on_chunk, on_retry, handle.tools
        )
        if isinstance(raw, list):
            reply = _tool_calls_reply(_parse_tool_calls(raw, self.name))
            # Mutate only after success. The assistant tool_calls turn itself is
            # parked on the handle rather than appended — see pending_tool_calls.
            handle.messages.append({"role": "user", "content": text})
            handle.pending_tool_calls = raw
            return reply
        if handle.tools:
            # Tool mode, but the model answered text anyway despite
            # tool_choice: "required". Never nudge for a sentinel here — that
            # contract does not apply to a tool session; hand it back for
            # tool_loop.py to abandon to rung 3.
            reply = _text_reply_in_tool_mode(raw)
            handle.messages.append({"role": "user", "content": text})
            handle.messages.append({"role": "assistant", "content": reply.raw})
            return reply
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            handle.system_prompt, pending_messages, raw, schema, on_chunk, on_retry
        )
        # Mutate only after success so handle stays consistent on error
        handle.structured_enabled = structured_enabled
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": reply.raw})
        return reply

    async def send_tool_results(
        self, handle: SessionHandle, results: list[ToolResult]
    ) -> AgentReply:
        """Continue a native tool-mode session with the outcomes of one round of calls.

        Appends the assistant turn carrying the provider's own ``tool_calls`` array
        (replayed verbatim from ``handle.pending_tool_calls``) followed by one
        ``{"role": "tool", "tool_call_id": ..., "content": <json>}`` message per
        result, and POSTs with ``tools`` still attached so the model can call again.

        Handle state is mutated only after a successful call, the same discipline
        ``send_message`` uses. A reply that is NOT another tool-call batch is handed
        back as-is for ``tool_loop.py`` to abandon to rung 3 — see ``_text_reply_in_
        tool_mode``.
        """
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        if not handle.tools:
            # A caller bug, not a provider rejection — the same guard anthropic_api.py,
            # gemini_api.py and opencode_zen.py already carry. Without it the request
            # goes out with role="tool" rows, an empty assistant tool_calls array and
            # NO tools field, 400s, and — because tools_fields_present is False —
            # classifies as a plain AgentBackendUnavailable, costing the job a BF-19
            # backend hop over a local mistake instead of failing loudly here.
            raise ValueError(
                "send_tool_results requires a native tool session — this handle has "
                "no tools (was it restored without tools=?)"
            )
        assistant_turn = {
            "role": "assistant",
            "content": None,
            "tool_calls": handle.pending_tool_calls or [],
        }
        result_turns = [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": json.dumps(result.content),
            }
            for result in results
        ]
        pending_messages = handle.messages + [assistant_turn, *result_turns]
        raw = await self._call_api(
            handle.system_prompt, pending_messages, None, None, None, handle.tools
        )
        if isinstance(raw, list):
            reply = _tool_calls_reply(_parse_tool_calls(raw, self.name))
            handle.messages.extend([assistant_turn, *result_turns])
            handle.pending_tool_calls = raw
            return reply
        reply = _text_reply_in_tool_mode(raw)
        handle.messages.extend([assistant_turn, *result_turns])
        handle.messages.append({"role": "assistant", "content": reply.raw})
        handle.pending_tool_calls = None
        return reply

    async def _parse_structured_with_downgrade(
        self,
        system_prompt: str,
        messages: list[dict],
        raw: str,
        schema: dict[str, Any] | None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> tuple[AgentReply, bool]:
        """Parse ``raw`` per the session's current mode; downgrade to sentinel mode
        on an unparseable/missing-``kind`` structured reply. See
        ``opencode_zen.py``'s method of the same name for the full rationale — this
        is behaviourally identical, just backend-name-agnostic in its log message.
        """
        if schema is None:
            return await self._parse_with_nudge(system_prompt, messages, raw, on_chunk=on_chunk, on_retry=on_retry), False
        try:
            return parse_structured_reply_for_schema(raw, schema), True
        except ProtocolError as exc:
            logger.warning(
                "%s structured reply unparseable (%s) — downgrading this session "
                "to sentinel mode for its remaining turns",
                self.name, exc,
            )
            reply = await self._parse_with_nudge(
                system_prompt, messages, raw, nudge_text=_DOWNGRADE_NUDGE_TEXT,
                on_chunk=on_chunk, on_retry=on_retry,
            )
            return reply, False

    async def _parse_with_nudge(
        self,
        system_prompt: str,
        messages: list[dict],
        raw: str,
        nudge_text: str = _NUDGE_TEXT,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> AgentReply:
        """Try parse_reply(raw); on 'no sentinel block' ProtocolError, nudge once.
        Any other ProtocolError, or a second failure, is re-raised immediately.
        See ``opencode_zen.py``'s method of the same name for the full rationale.

        ``on_retry``, when given, is awaited right before the nudge replay — the
        first attempt may already have streamed a partial (now-stale) buffer;
        see jsa/agents/base.py's ``OnRetry`` docstring.
        """
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
            if on_retry is not None:
                await on_retry()
            nudge_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": nudge_text},
            ]
            raw2 = await self._call_api(system_prompt, nudge_messages, None, on_chunk, on_retry)
            return parse_reply(raw2)  # Propagate on second failure

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op for stateless REST API — just clear in-memory history."""
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> str | list[dict]:
        """POST to ``endpoint_url``, retrying transient overload/gateway failures on
        THIS backend up to ``_MAX_ATTEMPTS`` before giving up. See
        ``opencode_zen.py``'s method of the same name for the full rationale.

        A ``_CacheRejected`` (a permanent 4xx while cache_control fields were
        present) is caught here, OUTSIDE ``retry_transient``'s budget — mirroring
        the structured->sentinel downgrade's rule that a reshaped retry never
        shares a budget with retries of the identical request. Prompt caching is
        disabled for the rest of this backend instance's life and the call is
        retried exactly once, clean.

        ``on_chunk`` is forwarded to ``_call_api_once`` for a real SSE stream in
        BOTH modes — ``response_format`` + ``stream: true`` is a normal, supported
        combination on this wire shape (see ``_call_api_once``), and some routed
        models expose a genuine ``reasoning_content`` delta even under a forced
        JSON schema. The ``content`` delta itself is raw partial JSON while
        structured, though, and a stray ``{`` is not useful to show — so in
        structured mode ``on_chunk`` is wrapped to forward ``reasoning`` chunks
        only, never ``content``; the wire-retry/self-heal machinery still gets the
        complete body regardless, via ``_consume_sse``'s own accumulation. Sentinel
        mode passes ``on_chunk`` through unwrapped, as before this filtering
        existed. ``on_retry`` is forwarded to ``retry_transient`` (the in-backend
        transient-HTTP retry loop) unconditionally too, so a retried structured
        call still discards any reasoning streamed by the abandoned attempt — see
        that function's docstring.

        **Tool mode never streams.** ``jsa/pipeline/tool_loop.py`` never passes an
        ``on_chunk`` (see its module docstring), and this method additionally forces
        ``stream_cb`` to None whenever ``tools`` are attached. That is what makes
        ``_consume_sse``'s lack of ``delta.tool_calls`` handling correct rather than a
        gap: fragmented ``function.arguments`` deltas are unreachable by construction,
        so do NOT "fix" ``_consume_sse`` to reassemble them — the reply always arrives
        as one complete non-streamed body.
        """
        if tools:
            stream_cb = None
        elif structured_schema is not None:
            stream_cb = _reasoning_only(on_chunk)
        else:
            stream_cb = on_chunk
        retry_cb = on_retry

        # Conditional kwarg, the same convention stages.py uses for
        # ``structured_schema``/``on_chunk``: ``tools=`` is offered ONLY when there is
        # a vocabulary to offer, so a subclass whose ``_call_api_once`` override is
        # not tool-aware (it may talk a wire shape with no OpenAI-compatible ``tools``
        # field at all — GeminiBackend replaces this method wholesale) is never handed
        # a parameter it cannot accept.
        tools_kwarg: dict[str, Any] = {"tools": tools} if tools else {}

        async def _attempt() -> str | list[dict]:
            return await retry_transient(
                lambda: self._call_api_once(
                    system_prompt, messages, structured_schema, stream_cb, **tools_kwarg
                ),
                unavailable_message=f"{self.name} API still failing after {_MAX_ATTEMPTS} attempts",
                on_retry=retry_cb,
            )

        # At most two degrades (reasoning, then caching), each retried exactly once
        # clean. Once a flag is off its raise site can no longer fire, so a repeat
        # failure surfaces as a plain AgentBackendUnavailable for BF-19, exactly as
        # it did before either degrade existed.
        #
        # There is deliberately NO third `except _ToolsRejected` branch here, and one
        # must never be added. Unlike caching/reasoning — optional enrichments with no
        # functional fallback, which therefore have to shed in-process — tool mode's
        # fallback is owned by a different layer entirely (tool_loop.py's native ->
        # prompt rung ladder, which rebuilds the session with a tool-contract system
        # prompt and no wire tools). Retrying clean here would send that same request
        # minus its tools and hand the result back to the loop as if nothing changed.
        # So _ToolsRejected PROPAGATES out of this backend, unretried. It subclasses
        # ToolsUnsupported (NOT AgentBackendUnavailable) precisely so it cannot be
        # caught by either branch below, nor mistaken for a BF-19 signal — see its
        # class docstring above.
        for _ in range(2):
            try:
                return await _attempt()
            except _ReasoningRejected as exc:
                if not self._reasoning:
                    raise
                logger.warning(
                    "%s rejected the reasoning opt-in field (%s) — disabling the "
                    "reasoning stream for this backend instance and retrying once "
                    "without it",
                    self.name, exc,
                )
                self._reasoning = False
            except _CacheRejected as exc:
                if not self._prompt_caching:
                    raise
                logger.warning(
                    "%s rejected the prompt-cache_control field (%s) — disabling prompt "
                    "caching for this backend instance and retrying once without it",
                    self.name, exc,
                )
                self._prompt_caching = False
        return await _attempt()

    async def _consume_sse(self, response: httpx.Response, on_chunk: OnChunk) -> tuple[str, str]:
        """Drain an SSE ``chat/completions`` stream, emitting content/reasoning
        deltas through ``on_chunk`` as they arrive, and returning
        ``(joined_content_text, raw_body_text)`` (never fabricates a reasoning
        chunk a model didn't send).

        ``raw_body_text`` is every line received, joined back together
        regardless of whether it was ``data:``-prefixed or parsed as a delta —
        it exists so the caller can recover and classify a genuine error when
        the "stream" wasn't SSE-shaped at all (this API can return an HTTP 200
        response whose body is a single JSON error envelope, not a real SSE
        stream — see the module docstring's "HTTP 200 with an error payload"
        note)."""
        content_parts: list[str] = []
        raw_lines: list[str] = []
        async for line in response.aiter_lines():
            if line:
                raw_lines.append(line)
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece, inline_reasoning = _split_content_delta(delta.get("content"))
            if piece:
                content_parts.append(piece)
                await on_chunk(AgentChunk(kind="content", text=piece))
            reasoning_piece = inline_reasoning + _reasoning_delta_text(delta)
            if reasoning_piece:
                await on_chunk(AgentChunk(kind="reasoning", text=reasoning_piece))
        return "".join(content_parts), "".join(raw_lines)

    async def _call_api_once(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> str | list[dict]:
        """Single POST to the chat-completions endpoint; classifies and raises on
        any failure. Never retries itself — see _call_api. Mirrors
        ``opencode_zen.py``'s method of the same name; see its docstring for the
        full rationale behind each classification branch.

        Returns the assistant's text, OR — when ``tools`` were attached and the model
        answered with a native call batch — the RAW ``message.tool_calls`` array. The
        raw wire entries (not parsed ``ToolCall``s) are returned deliberately:
        ``send_tool_results`` has to replay that exact array back as the assistant
        turn its ``role="tool"`` results answer.
        """
        assert not (tools and structured_schema is not None), (
            "tool mode and structured mode are mutually exclusive per request — the "
            "terminal tool's arguments ARE the structured output, so no "
            "response_format may be sent alongside tools"
        )
        api_key = self._api_key()
        system_content = self._system_content(system_prompt)
        cache_fields_present = isinstance(system_content, list)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system_content}, *messages],
            "max_tokens": 32000,
        }
        payload.update(self._extra_payload(system_prompt))
        reasoning_payload = self._reasoning_payload()
        reasoning_fields_present = bool(reasoning_payload)
        payload.update(reasoning_payload)
        tools_fields_present = bool(tools)
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = "required"
        if structured_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _RESPONSE_FORMAT_NAME,
                    # Not strict — see opencode_zen.py's module docstring: the turn
                    # models' nested $defs are deliberately not recursively strict.
                    "strict": False,
                    "schema": structured_schema,
                },
            }
        use_stream = on_chunk is not None
        if use_stream:
            payload["stream"] = True
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        streamed_content: str | None = None
        streamed_raw: str = ""
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            if use_stream:
                async with client.stream(
                    "POST", self.endpoint_url, headers=headers, json=payload
                ) as response:
                    if response.status_code == 200:
                        streamed_content, streamed_raw = await self._consume_sse(response, on_chunk)
                    else:
                        # Not a stream — read the full (error) body for classification
                        # below, exactly as the non-streaming branch would receive it.
                        await response.aread()
            else:
                response = await client.post(
                    self.endpoint_url, headers=headers, json=payload,
                )
        except httpx.TimeoutException:
            raise AgentTimeout(
                f"{self.name} API timed out after {self._timeout}s"
            ) from None
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self.name} API transport error: {exc}"
            ) from None
        finally:
            await client.aclose()

        def _permanent_4xx(detail: str) -> Exception:
            # Degrade precedence: tools -> reasoning -> caching -> fail.
            #
            # Tools shed FIRST for two independent reasons: `tools`/`tool_choice` is
            # the newest and least-universally-supported field on this wire shape, so
            # it is the likeliest culprit; and unlike the two enrichments it has a
            # real functional fallback (tool_loop.py's prompt rung still patches the
            # document), so the loss is recoverable rather than simply gone. Note the
            # asymmetry this creates and keep it: _ToolsRejected is NOT retried clean
            # in-process the way the other two are — it propagates to tool_loop.py,
            # which owns the rung ladder (see _call_api's comment and _ToolsRejected's
            # docstring).
            #
            # Reasoning then caching: both are optional enrichments, and _call_api
            # degrades them one at a time, retrying clean after each. If the rejection
            # was really about the OTHER field, dropping reasoning first simply costs
            # one extra attempt before the cache degrade fires.
            if tools_fields_present:
                return _ToolsRejected(detail)
            if reasoning_fields_present:
                return _ReasoningRejected(detail)
            if cache_fields_present:
                return _CacheRejected(detail)
            return AgentBackendUnavailable(detail)

        if response.status_code == 429:
            raise AgentLimitReached(f"{self.name} API rate limit reached: {response.text[:500]}")

        if streamed_content:
            return streamed_content

        if streamed_content == "":
            # HTTP 200, streamed, but no delta content was extracted. Before
            # assuming a generic transient/empty-stream failure, try to recover
            # the raw stream body as a JSON error envelope -- this API can
            # return a 200-status response whose body is a single JSON error
            # object rather than a real SSE stream (see module docstring), which
            # _consume_sse's "data:"-only parsing would otherwise silently drop.
            try:
                body = json.loads(streamed_raw) if streamed_raw else None
            except ValueError:
                body = None
            if not isinstance(body, dict) or "error" not in body:
                raise TransientBackendError(
                    f"{self.name} API stream produced no content (model may have "
                    "produced only reasoning tokens before hitting max_tokens)"
                )
            # Fall through to the shared "error" in body classification below,
            # using the body recovered from the stream instead of response.json().
        else:
            try:
                body = response.json()
            except ValueError:
                detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
                if response.status_code >= 500:
                    raise TransientBackendError(detail) from None
                raise _permanent_4xx(detail) from None

        if "error" in body:
            err = body["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if "rate" in message.lower() or "credit" in message.lower() or err_type in {"RateLimitError", "CreditsError"}:
                raise AgentLimitReached(f"{self.name} API limit reached: {message}")
            if 400 <= response.status_code < 500:
                raise _permanent_4xx(f"{self.name} API error: {message}")
            raise TransientBackendError(f"{self.name} API error: {message}")

        if response.status_code >= 400:
            detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail)
            raise _permanent_4xx(detail)

        choices = body.get("choices")
        if not choices:
            raise TransientBackendError(
                f"{self.name} API returned no choices: {response.text[:500]}"
            )
        message = choices[0].get("message") or {}
        _log_cached_tokens(self.name, body)
        raw_tool_calls = message.get("tool_calls")
        # Checked BEFORE the null-content branch below, and this order is load-bearing:
        # a native tool-call reply legitimately carries `content: null` alongside its
        # `tool_calls`, so the transient "null message content" classification would
        # otherwise burn all _MAX_ATTEMPTS retries on a perfectly good tool call and
        # end as AgentBackendUnavailable, dropping the backend out of BF-19.
        if isinstance(raw_tool_calls, list) and raw_tool_calls:
            return raw_tool_calls
        content = message.get("content")
        if isinstance(content, list):
            # Mistral with reasoning_effort set returns a chunk LIST here, not a
            # string — keep only the answer text, same split the SSE path uses.
            content = _split_content_delta(content)[0] or None
        if content is None:
            raise TransientBackendError(
                f"{self.name} API returned null message content (model may have "
                "produced only reasoning tokens before hitting max_tokens)"
            )
        return content
