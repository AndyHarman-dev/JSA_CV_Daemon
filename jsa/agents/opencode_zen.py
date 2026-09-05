"""OpenCodeZenBackend: OpenAI-compatible chat-completions HTTP backend.

Talks to https://opencode.ai/zen/v1/chat/completions — an OpenAI-compatible
endpoint (not the Anthropic Messages API shape). Unlike AnthropicAPIBackend's
`system` top-level kwarg, the system prompt here is just the first message in
the `messages` array with `role: "system"`.

The API key is read from the `OPENCODE_API_KEY` environment variable (never
hardcoded, never logged) at call time, mirroring how `anthropic.AsyncAnthropic()`
picks up `ANTHROPIC_API_KEY` implicitly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

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

_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

# Fixed response_format schema name — mirrors AnthropicAPIBackend's fixed tool name
# ("respond"): the backend layer is stage-agnostic, so there is no per-stage name to
# use here either.
_RESPONSE_FORMAT_NAME = "structured_reply"

# The free-tier models this backend proxies (nemotron-3-ultra-free by default) are
# known to be flaky under upstream load: intermittent 5xx gateway errors, a JSON
# error envelope at a non-4xx status, or a null message content. These are treated
# as transient — the SAME backend often succeeds on the very next call — so
# _call_api retries them in-process up to _MAX_ATTEMPTS before giving up. This is
# distinct from a bad model name / auth error / rate limit, none of which a retry
# can fix — those raise immediately (see _call_api_once) so BF-19 switches
# backends without wasting the retry budget on a config problem.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 3.0)  # sleep before attempt 2 and attempt 3


class _TransientOpenCodeError(Exception):
    """Internal only: signals a single attempt's retryable overload/gateway
    failure to the retry loop in _call_api. Never escapes this module —
    _call_api converts an exhausted retry budget into AgentBackendUnavailable."""


class _ToolsRejected(ToolsUnsupported):
    """A permanent 4xx from ``_call_api_once`` while native tool definitions were
    actually present in the payload (``tools`` + ``tool_choice``).

    This file's own copy of the class ``_openai_compat.py`` defines under the same
    name — deliberately duplicated, never imported from there, exactly like
    ``_TransientOpenCodeError`` above (see this module's docstring and CLAUDE.md's
    "the four multi-backend-model-select backends": ``opencode_zen.py`` keeps its own
    independent copy of the shared machinery rather than being wired onto that base).

    **Subclasses ``ToolsUnsupported``, NOT ``AgentBackendUnavailable``** — and unlike
    every other degrade in this codebase it is NOT caught-and-retried-clean in-process.
    The revision-tool-use plan's Phase 3 preamble sketches the ``_CacheRejected`` mirror
    (subclass ``AgentBackendUnavailable``, catch it locally, retry once without the
    field); that analogy is wrong here, for two independent reasons:

    1. Caching and reasoning are optional enrichments with NO functional fallback, so
       they must shed in-backend. Tool mode DOES have a fallback, and it is owned by a
       different layer entirely — ``jsa/pipeline/tool_loop.py``'s native -> prompt rung
       ladder, which re-establishes the session with a tool-contract system prompt and
       no wire tools at all. Retrying clean in-process would instead send a
       tool-contract system prompt with no tools attached and hand the resulting
       sentinel reply back to the loop: semantically incoherent.
    2. If this subclassed ``AgentBackendUnavailable``, an escaped instance would tell
       BF-19 to advance the whole job to the next configured backend over a tools-only
       degrade — exactly the loss the rung ladder exists to prevent. See
       ``ToolsUnsupported``'s docstring in ``jsa/agents/base.py``.

    Consequence: ``_call_api``'s ``_MAX_ATTEMPTS`` retry loop needs **no** change and
    must never grow a branch for this. ``retry_transient``-style loops re-raise
    non-transient exceptions, this one is raised from ``_call_api_once`` outside the
    ``_TransientOpenCodeError`` family, and this backend has no local degrade loop to
    teach — so it simply propagates out of the backend to ``tool_loop.py``, which
    downgrades that turn to the prompt rung.
    """


def _permanent_4xx(
    detail: str, *, tools_present: bool
) -> AgentBackendUnavailable | _ToolsRejected:
    """The exception for a permanent (unretryable) 4xx: ``_ToolsRejected`` when native
    tool fields were actually in the payload, plain ``AgentBackendUnavailable``
    otherwise.

    Gated on the payload shape actually sent, never on a capability flag — a non-tool
    turn's bad-model/auth 4xx must keep engaging BF-19 exactly as it did before native
    tools existed. See ``_call_api_once``'s docstring for why this diverges from the
    structured-output 4xx, which is deliberately NOT special-cased.
    """
    if tools_present:
        return _ToolsRejected(detail)
    return AgentBackendUnavailable(detail)


def _reject_tools_with_schema(
    tools: tuple[ToolSpec, ...] | None, structured_schema: dict[str, Any] | None
) -> None:
    """Tool mode and structured mode are mutually exclusive per request.

    The terminal tool's arguments ARE the structured output, so a session that has
    tools must never also carry a ``response_format`` — nobody has probed how this
    proxy behaves with both, and no configuration ever wants both (the prompt rung IS
    the sentinel path). Raised at session-establishment time so a caller bug surfaces
    with a name rather than as a confusing wire-level 4xx; ``_call_api_once`` asserts
    the same invariant per request, at the payload it actually builds.
    """
    if tools is not None and structured_schema is not None:
        raise ValueError(
            "OpenCodeZenBackend: native tools and structured_schema are mutually "
            "exclusive — the terminal tool's arguments are the structured output"
        )


def _tool_calls_from_wire(wire_calls: list[Any]) -> list[ToolCall]:
    """Normalize an OpenAI-shape ``message.tool_calls`` array into ``ToolCall``s.

    Each entry carries ``id`` and ``function.{name, arguments}``, where ``arguments`` is
    a JSON **string** on this wire shape — decoded here so ``ToolCall.arguments`` is
    always a plain dict, never a string awaiting a second parse (see ``ToolCall``'s
    docstring in ``jsa/agents/base.py``). The provider-issued ``id`` is preserved
    verbatim; ``call_{i}`` is synthesized only if the proxy omitted one.

    A malformed entry raises ``ProtocolError`` — the model sent something unusable, the
    transport was fine, so this is deliberately NOT a transient/limit/unavailable
    signal and never touches the retry loop.
    """
    calls: list[ToolCall] = []
    for i, entry in enumerate(wire_calls):
        function = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(function, dict):
            raise ProtocolError(f"malformed tool_calls entry {i}: no 'function' object")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(f"malformed tool_calls entry {i}: missing tool name")
        raw_args = function.get("arguments")
        if raw_args is None or raw_args == "":
            arguments: Any = {}
        elif isinstance(raw_args, dict):
            # Some proxies hand back an already-decoded object rather than a string.
            arguments = raw_args
        elif isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except ValueError as exc:
                raise ProtocolError(
                    f"malformed tool_calls entry {i} ({name}): arguments is not valid "
                    f"JSON ({exc})"
                ) from exc
        else:
            raise ProtocolError(
                f"malformed tool_calls entry {i} ({name}): arguments must be a JSON "
                "object (or a JSON-object string)"
            )
        if not isinstance(arguments, dict):
            raise ProtocolError(
                f"malformed tool_calls entry {i} ({name}): arguments must decode to an "
                "object"
            )
        call_id = entry.get("id")
        calls.append(
            ToolCall(
                id=call_id if isinstance(call_id, str) and call_id else f"call_{i}",
                name=name,
                arguments=arguments,
            )
        )
    return calls

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
# holds before restating the sentinel grammar — plain _NUDGE_TEXT's "was missing the
# required sentinel block" would contradict what the model was actually instructed
# to do moments ago.
_DOWNGRADE_NUDGE_TEXT = (
    "Disregard the structured-output/JSON-schema contract from earlier in this "
    "session — it no longer applies. Restate your previous response using the "
    "sentinel-block format instead, and end it with exactly one of:\n"
    "<<<NEED_INPUT>>>\n<your question>\n<<<END>>>\n"
    "or\n"
    "<<<FINAL>>>\n<your final content>\n<<<END>>>"
)


@dataclass(kw_only=True)
class OpenCodeZenSessionHandle(SessionHandle):
    """Session handle for OpenCodeZenBackend; carries conversation history in memory."""
    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can rebuild the messages array
    messages: list[dict] = field(default_factory=list)  # growing conversation list (no system entry)
    # The schema established at start/restore time (None = this session never
    # attempted structured mode — byte-identical to pre-Phase-4 behavior).
    structured_schema: dict[str, Any] | None = None
    # Per-session downgrade flag (NEVER persisted — a restart re-attempts structured,
    # see the module docstring / structured-output plan's Locked decision #2). Only
    # meaningful when structured_schema is not None; flips permanently to False the
    # first time a structured reply is unparseable. Established at start/restore time
    # from whether a schema was actually supplied — never defaulted to True
    # unconditionally, or a restored session would claim structured mode while
    # stages.py may have replayed sentinel-form history into it.
    structured_enabled: bool = False
    # Native tool vocabulary for this session (revision-tool-use plan, Phase 3/E3).
    # None = ordinary sentinel/structured session, byte-identical to pre-tool
    # behavior. When set, every POST in this session carries `tools` +
    # `tool_choice: "required"` and NEVER `response_format` (the two are mutually
    # exclusive per request — the terminal tool's arguments ARE the structured
    # output), so structured_schema/structured_enabled above stay inert: a
    # tool-mode session was never in structured mode and can never downgrade out
    # of it.
    tools: tuple[ToolSpec, ...] | None = None


def _reasoning_only(on_chunk: OnChunk | None) -> OnChunk | None:
    """Wrap ``on_chunk`` so only ``kind="reasoning"`` chunks pass through — used
    for structured-schema calls, where the ``content`` delta is raw partial JSON
    (not useful to render as chat text) but a ``reasoning_content`` delta, when a
    routed model exposes one, still is. ``None`` in, ``None`` out. Identical to
    ``_openai_compat.py``'s copy — see this module's docstring for why this file
    keeps its own independent implementation rather than sharing that base."""
    if on_chunk is None:
        return None

    async def _filtered(chunk: AgentChunk, _cb: OnChunk = on_chunk) -> None:
        if chunk.kind == "reasoning":
            await _cb(chunk)

    return _filtered


def _active_schema(
    handle: OpenCodeZenSessionHandle, explicit: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The schema actually in force for one call: None once the session has
    downgraded, regardless of what the caller (stages.py) keeps passing in —
    stages.py has no visibility into this backend-internal downgrade, so it will
    keep supplying the stage's schema on every call; this is what makes that
    inert post-downgrade."""
    if not handle.structured_enabled:
        return None
    return explicit if explicit is not None else handle.structured_schema


class OpenCodeZenBackend(AgentBackend):
    """AgentBackend implementation that calls the OpenCode Zen chat-completions API."""

    name = "opencode-zen"
    supports_structured_output = True
    # SSE stream via `stream: true`, same approach as _openai_compat.py — but this
    # module deliberately keeps its own copy rather than sharing that base (see the
    # module docstring). Never attempted in structured mode.
    supports_streaming = True
    # Native `tools` + `tool_choice: "required"` on the /chat/completions shape.
    # Contract (jsa/agents/base.py): tools= is accepted on start_session and
    # restore_session, and send_tool_results is overridden below.
    supports_native_tools = True

    def __init__(self, model: str = "nemotron-3-ultra-free", timeout: float = 180.0) -> None:
        self._model = model
        self._timeout = timeout

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> tuple[OpenCodeZenSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply.

        ``tools`` is accepted for the ``supports_native_tools`` contract in
        ``jsa/agents/base.py``; in practice ``tool_loop.py`` always restores rather
        than starts (revisions replay history), so this branch exists for completeness
        and for any future caller, not because the pipeline uses it today.
        """
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        if tools is not None:
            _reject_tools_with_schema(tools, structured_schema)
            reply, assistant_msg = await self._tool_turn(system_prompt, messages, tools)
            messages.append(assistant_msg)
            return (
                OpenCodeZenSessionHandle(
                    id=str(uuid.uuid4()),
                    external_id=None,
                    system_prompt=system_prompt,
                    messages=messages,
                    structured_schema=None,
                    structured_enabled=False,
                    tools=tools,
                ),
                reply,
            )
        raw = await self._call_api(system_prompt, messages, structured_schema, on_chunk, on_retry)
        assert isinstance(raw, str)  # no tools were sent, so never a tool_calls array
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            system_prompt, messages, raw, structured_schema, on_chunk, on_retry
        )
        messages.append({"role": "assistant", "content": reply.raw})
        handle = OpenCodeZenSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_enabled,
        )
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> OpenCodeZenSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        The session's structured mode is (re)established here from the caller's
        schema argument, mirroring AnthropicSessionHandle — never defaulted to
        True unconditionally (see OpenCodeZenSessionHandle.structured_enabled).

        ``tools`` (``jsa/pipeline/tool_loop.py``'s native rung) puts the session in
        native tool mode instead: structured mode is left off for its whole life, since
        the two are mutually exclusive per request. Supplying both is a caller bug, not
        a degrade — it raises rather than silently picking one.
        """
        _reject_tools_with_schema(tools, structured_schema)
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = OpenCodeZenSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
            tools=tools,
        )
        return handle

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
        call, keeping the list coherent if the call times out or raises. The
        effective schema (see _active_schema) collapses to None once the session
        has downgraded, regardless of what the caller keeps passing in.
        """
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
            )
        if handle.tools is not None:
            # Native tool mode. Deliberately shares nothing with the structured path
            # below: no response_format (mutually exclusive per request), no
            # _parse_structured_with_downgrade, and no _parse_with_nudge — a tool-mode
            # session was never in structured mode, so `structured_enabled` is not a
            # meaningful thing to downgrade here, and a sentinel nudge would be
            # answering a contract this session was never given.
            pending_tool_messages = handle.messages + [{"role": "user", "content": text}]
            reply, assistant_msg = await self._tool_turn(
                handle.system_prompt, pending_tool_messages, handle.tools
            )
            # Mutate only after success, same discipline as the sentinel path below.
            handle.messages.append({"role": "user", "content": text})
            handle.messages.append(assistant_msg)
            return reply
        schema = _active_schema(handle, structured_schema)
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages, schema, on_chunk, on_retry)
        assert isinstance(raw, str)  # no tools were sent, so never a tool_calls array
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            handle.system_prompt, pending_messages, raw, schema, on_chunk, on_retry
        )
        # Mutate only after success so handle stays consistent on error
        handle.structured_enabled = structured_enabled
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": reply.raw})
        return reply

    async def _tool_turn(
        self,
        system_prompt: str,
        pending_messages: list[dict],
        tools: tuple[ToolSpec, ...],
    ) -> tuple[AgentReply, dict]:
        """One POST in native tool mode. Returns ``(reply, assistant_message)`` — the
        caller appends that message to ``handle.messages`` only on success.

        Never passes ``on_chunk``: tool mode never streams (the revision-tool-use
        plan's Phase 2 finding #1), which is what makes reassembling a fragmented
        ``delta.tool_calls[].function.arguments`` in ``_consume_sse`` unreachable
        rather than merely unimplemented — see the comment at ``use_stream``.

        A reply with no tool calls at all is returned as a nominal ``kind="final"``
        carrying the model's prose verbatim, NOT run through ``parse_reply``: a
        tool-mode session was told to answer with tools, so prose is a broken tool
        turn, and ``tool_loop.py`` discards any non-``tool_calls`` reply (downgrading
        to the prompt rung at loop entry, or abandoning to rung 3 mid-loop). Parsing
        it would only add a raise path to a reply that is discarded either way.
        """
        raw = await self._call_api(system_prompt, pending_messages, None, None, None, tools=tools)
        if isinstance(raw, list):
            calls = _tool_calls_from_wire(raw)
            # Canonical, provider-neutral text — the same JSON array shape the prompt
            # rung's <<<TOOL_CALLS>>> body carries (jsa/agents/protocol.py), never the
            # OpenCode Zen envelope. The wire entries themselves go back on the
            # assistant message, which is what the next POST must replay.
            content = json.dumps([{"name": c.name, "arguments": c.arguments} for c in calls])
            reply = AgentReply(raw=content, content=content, kind="tool_calls", tool_calls=calls)
            return reply, {"role": "assistant", "content": None, "tool_calls": raw}
        return AgentReply(raw=raw, content=raw, kind="final"), {
            "role": "assistant",
            "content": raw,
        }

    async def send_tool_results(
        self, handle: SessionHandle, results: list[ToolResult]
    ) -> AgentReply:
        """Continue a native tool-mode session with the outcomes of the last round of
        tool calls (``jsa/agents/base.py``'s ``supports_native_tools`` contract).

        Appends one ``{"role": "tool", "tool_call_id": ..., "content": ...}`` message
        per result, in order and unfiltered — ``tool_loop.py`` synthesizes a result for
        every call it saw, including ``not_executed``/``budget_exhausted`` ones, and a
        strict proxy 400s if any ``tool_call_id`` from the assistant turn goes
        unanswered. ``call_id`` is used verbatim, never re-derived. The assistant turn
        carrying ``tool_calls`` is already in ``handle.messages`` (appended by
        ``send_message``/``_tool_turn`` on the round that produced these calls), so it
        precedes these rows on the wire, as this shape requires.
        """
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
            )
        if handle.tools is None:
            # Never silently POST tool results with no tools attached — that would be
            # an incoherent request, and it would hide the real bug (a caller that
            # reached the tool loop through a session opened without tools).
            raise RuntimeError(
                "send_tool_results called on an OpenCode Zen session that was not "
                "opened with tools"
            )
        if not (handle.messages and handle.messages[-1].get("tool_calls")):
            # `role: "tool"` rows are only legal on this wire shape immediately after
            # an assistant turn carrying the matching tool_call_ids. tool_loop.py can
            # never violate this (it only calls here from inside a
            # `while reply.kind == "tool_calls"` round), but without this check the
            # violation would surface as a strict proxy's 400 — classified as
            # _ToolsRejected and silently degrading to the prompt rung for entirely
            # the wrong reason.
            raise RuntimeError(
                "send_tool_results requires the preceding assistant turn to carry "
                "tool_calls; none found on this OpenCode Zen session"
            )
        tool_messages = [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": json.dumps(result.content),
            }
            for result in results
        ]
        pending_messages = handle.messages + tool_messages
        reply, assistant_msg = await self._tool_turn(
            handle.system_prompt, pending_messages, handle.tools
        )
        # Mutate only after a successful call, matching send_message's discipline.
        handle.messages.extend(tool_messages)
        handle.messages.append(assistant_msg)
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
        on an unparseable/missing-``kind`` structured reply.

        Called strictly AFTER ``_call_api`` has already returned successfully — a
        transient/quota/timeout failure never reaches this method and therefore
        never touches the downgrade flag (that classification lives entirely in
        ``_call_api``/``_call_api_once`` and is untouched by this method). Only a
        parse-layer ``ProtocolError`` from ``parse_structured_reply_for_schema``
        (invalid JSON or a missing/invalid ``kind`` — see turn_models.py) triggers
        a downgrade; that function deliberately does not run CV/cover-letter
        semantic validation, so a real content problem in the payload never
        reaches here either — it surfaces downstream in `_validate_final_content`
        exactly like the sentinel path's failures do.

        ``schema is None`` means this call was never in structured mode to begin
        with (never requested, or already downgraded by an earlier turn in this
        session) — go straight to the plain sentinel path, byte-identical to
        pre-Phase-4 behavior.

        Returns ``(reply, structured_enabled)`` — the caller persists the second
        value onto the handle.

        Edge case, deliberately left as-is rather than special-cased: if ``raw``
        itself contains an open ``<<<`` marker with no ``<<<END>>>`` (a model
        confusedly mixing sentinel text into its malformed JSON attempt),
        ``_parse_with_nudge``'s first ``parse_reply(raw)`` call raises
        ``"unterminated block"`` instead of ``"no sentinel block"`` and
        re-raises immediately (its own "any other ProtocolError" rule) —
        propagating out of this method BEFORE the ``return reply, False`` below
        ever runs, so the caller's ``handle.structured_enabled`` write is
        skipped and the session stays structured for its next turn. This is
        acceptable, not a bug: the ProtocolError still propagates to Phase 5's
        self-heal budget exactly as an ordinary structured-mode failure would,
        it just means this specific double-malformed shape doesn't also
        downgrade. Also applies uniformly to the fit-verdict schema — this
        method has no per-stage knowledge, so a malformed fit reply nudges
        (and thus costs a second API call) exactly like cv/cl turns do, even
        though `_run_fit_assessment` treats the stage as one-shot; see
        ``TestStructuredFitVerdict.test_malformed_fit_reply_still_nudges`` in
        tests/backend/test_opencode_zen.py, pinned for Phase 5's attention.
        """
        if schema is None:
            return await self._parse_with_nudge(system_prompt, messages, raw, on_chunk=on_chunk, on_retry=on_retry), False
        try:
            return parse_structured_reply_for_schema(raw, schema), True
        except ProtocolError as exc:
            logger.warning(
                "OpenCode Zen structured reply unparseable (%s) — downgrading this "
                "session to sentinel mode for its remaining turns",
                exc,
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

        The free-tier models this backend proxies (e.g. the default
        ``nemotron-3-ultra-free``) are known to occasionally answer in plain prose —
        including asking their NEED_INPUT-shaped question — without ever wrapping it
        in the mandatory sentinel block (observed live: ``finish_reason: "stop"``, so
        this is non-compliance, not truncation). Unlike ClaudeCliBackend, this REST
        backend has no server-side session to `--resume`, so the retry replays the
        full message list (plus the bad reply and a nudge turn) in one more POST.
        Mirrors ClaudeCliBackend._parse_with_nudge's contract: the returned
        AgentReply's `.raw` is what start_session/send_message persist as the
        assistant turn, so a successful nudge is transparent to the caller — the
        intermediate nudge exchange itself is not persisted, same as the CLI backend.
        Any other ProtocolError, or a second failure, is re-raised immediately.

        Also reached, via ``_parse_structured_with_downgrade``, on a structured-mode
        reply that never wrapped its bare JSON in sentinels at all — ``parse_reply``
        raises the same "no sentinel block" ProtocolError for that raw text, so the
        trigger check below fires identically; only ``nudge_text`` differs (a
        downgrade passes ``_DOWNGRADE_NUDGE_TEXT``). The follow-up POST below never
        passes a ``structured_schema`` — a downgrade's nudge call must stay in
        sentinel mode, and an already-sentinel-mode session never had one to omit.
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
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
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
    ) -> str | list[Any]:
        """POST to the OpenCode Zen endpoint, retrying transient overload/gateway
        failures on THIS backend up to _MAX_ATTEMPTS before giving up.

        Returns the reply text, or — in native tool mode only — the raw
        ``message.tool_calls`` array (normalized one layer up, in _tool_turn).

        A rate limit (AgentLimitReached), a timeout (AgentTimeout), or a permanent
        config/client error (AgentBackendUnavailable — bad model, bad key, malformed
        request) all propagate immediately, unretried: none of those are fixed by
        calling the same backend again, so retrying them would only burn the fit/
        cv_adjust/cover_letter stage's wall-clock budget before BF-19 ever gets a
        chance to switch backends. Only the free-tier's known-flaky overload/gateway
        signals (raised as _TransientOpenCodeError by _call_api_once) get retried here.
        ``structured_schema`` (when not None) is forwarded unchanged to every retry
        attempt — this loop retries the SAME request, never changes its shape; the
        structured→sentinel downgrade lives one layer up, entirely outside this loop
        (see _parse_structured_with_downgrade), so a malformed structured reply never
        burns retry budget here and a transient HTTP failure never touches the
        downgrade flag.

        ``tools`` is likewise forwarded unchanged to every attempt. This loop needs
        **no** branch for _ToolsRejected and must never grow one: it catches only
        _TransientOpenCodeError, so a tools-present permanent 4xx propagates straight
        out of the backend after exactly one attempt, to tool_loop.py's native ->
        prompt rung ladder. That ladder — a different layer, re-establishing the
        session with a tool-contract prompt and no wire tools — is the degrade;
        re-issuing the same request here without tools would be incoherent. See
        _ToolsRejected's docstring.
        """
        # response_format + stream:true is a normal combination on this wire shape
        # (see _call_api_once) and some routed models expose a genuine
        # reasoning_content delta even under a forced JSON schema — so streaming
        # is attempted in BOTH modes. The content delta is raw partial JSON while
        # structured though (a stray "{" is not useful to show), so on_chunk is
        # wrapped to forward reasoning chunks only in that case; sentinel mode
        # passes it through unwrapped. on_retry is forwarded unconditionally too,
        # so a retried structured call still discards any reasoning streamed by
        # the abandoned attempt.
        stream_cb = _reasoning_only(on_chunk) if structured_schema is not None else on_chunk
        retry_cb = on_retry
        last_exc: _TransientOpenCodeError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await self._call_api_once(
                    system_prompt, messages, structured_schema, stream_cb, tools=tools
                )
            except _TransientOpenCodeError as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    logger.warning(
                        "OpenCode Zen API transient failure (attempt %d/%d), retrying: %s",
                        attempt + 1, _MAX_ATTEMPTS, exc,
                    )
                    if retry_cb is not None:
                        await retry_cb()
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                    continue
        raise AgentBackendUnavailable(
            f"OpenCode Zen API still failing after {_MAX_ATTEMPTS} attempts: {last_exc}"
        ) from None

    async def _consume_sse(self, response: httpx.Response, on_chunk: OnChunk) -> tuple[str, str]:
        """Drain a chat/completions SSE stream (identical wire shape to
        _openai_compat.py's copy — see the module docstring for why this file
        keeps its own independent copy rather than sharing that base).

        Returns ``(joined_content_text, raw_body_text)`` — ``raw_body_text`` is
        every line received, joined back together, so the caller can recover and
        classify a genuine HTTP-200 JSON error envelope (this backend's own
        documented failure mode) when the "stream" wasn't SSE-shaped at all."""
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
            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                await on_chunk(AgentChunk(kind="content", text=piece))
            reasoning_piece = delta.get("reasoning_content")
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
    ) -> str | list[Any]:
        """Single POST to the OpenCode Zen chat-completions endpoint; classifies
        and raises on any failure. Never retries itself — see _call_api.

        Returns the reply text, or the raw ``message.tool_calls`` array when native
        tools were sent and the model answered with calls.

        The client is created per-call and explicitly closed in a finally block,
        matching AnthropicAPIBackend's connection-pool hygiene. `await
        client.post(...)` is a real async operation, so Orchestrator.cancel_task()'s
        task.cancel() can interrupt it directly at this await point.

        A 4xx caused specifically by ``response_format``/``json_schema`` being
        unsupported by a given proxied model is, from the JSON error body alone,
        indistinguishable from a genuinely bad model name or malformed request —
        both are structured 4xx error bodies with no reliable ``type``/message
        marker to key off (the existing 3-way classification below already commits
        to never guessing at upstream error-type strings for exactly this reason).
        Deliberately NOT special-cased here: it is classified identically to any
        other 4xx, below — AgentBackendUnavailable, unretried, engaging BF-19 to
        switch backends. This is narrower than "every structured-output failure
        downgrades to sentinel" — a model that emits non-conforming JSON downgrades
        (see _parse_structured_with_downgrade), but a proxy that rejects
        response_format at the wire level takes the whole backend out of the
        fallback chain instead. Flagged for Phase 6's integration tests to observe
        whether this actually occurs against the free-tier proxy in practice.

        A tools-present 4xx **is** special-cased, and the divergence from the
        paragraph above is deliberate, not an inconsistency. The classification
        problem is identical — the JSON error body gives no reliable marker
        distinguishing "this model doesn't do tool calling" from a bad model name —
        but the right *answer* differs, because the available fallbacks differ.
        ``response_format`` failing means structured mode is simply dead for that
        model, and there is nothing better to do than hand the backend to BF-19. Tool
        mode has a strictly better LOCAL answer that did not exist when the paragraph
        above was written: rung 2, the prompt rung, on the SAME backend with the SAME
        session material, re-established by ``jsa/pipeline/tool_loop.py``. So a
        permanent 4xx here raises ``_ToolsRejected`` (a ``ToolsUnsupported``, NOT an
        ``AgentBackendUnavailable``) whenever tool fields were actually in the payload
        — spending one same-backend rung instead of a whole BF-19 hop. If that turns
        out not to have been the cause, the prompt-rung retry sends no tools, hits the
        same 4xx, and gets the ordinary ``AgentBackendUnavailable`` — so a
        misclassification costs one round-trip, never a BF-19 slot. Gating is on the
        payload actually sent (``tools is not None``), never on the capability flag:
        every non-tool turn's classification is byte-identical to what it was before
        native tools existed.
        """
        api_key = os.environ.get("OPENCODE_API_KEY", "")
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_tokens": 32000,
        }
        if structured_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": _RESPONSE_FORMAT_NAME,
                    # Not strict: the turn models' nested $defs (CVDocument/CoverLetter)
                    # are deliberately NOT recursively strict (extra="ignore", defaulted
                    # optional fields — see turn_models.py's module docstring), which a
                    # strict:true json_schema mode would reject. The per-session
                    # downgrade path already exists to handle a model/proxy that doesn't
                    # honor the schema, so strict:false costs nothing here.
                    "strict": False,
                    "schema": structured_schema,
                },
            }
        if tools is not None:
            # Tool mode and structured mode are mutually exclusive per request: the
            # terminal tool's arguments ARE the structured output. Asserted at the
            # payload we actually build, in addition to the session-establishment
            # guard in _reject_tools_with_schema.
            assert structured_schema is None, (
                "OpenCode Zen: tools and response_format must never be sent together"
            )
            payload["tools"] = to_openai_tools(tools)
            # "required" = the model must call at least one tool this turn. The
            # tool_loop's contract needs a call every round (finalize/ask_user are
            # themselves tools), so this is the forcing equivalent of Anthropic's
            # tool_choice {"type": "any"}.
            payload["tool_choice"] = "required"
        # Tool mode never streams (revision-tool-use plan, Phase 2 finding #1). No
        # caller passes on_chunk on the tool path, and `tools is None` makes that
        # structural rather than incidental: _consume_sse therefore never has to
        # reassemble a fragmented `delta.tool_calls[].function.arguments` — that
        # reassembly is UNREACHABLE by construction, not merely unimplemented. Do not
        # "fix" _consume_sse to parse tool_calls deltas.
        use_stream = on_chunk is not None and tools is None
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
                    "POST", _ENDPOINT, headers=headers, json=payload
                ) as response:
                    if response.status_code == 200:
                        streamed_content, streamed_raw = await self._consume_sse(response, on_chunk)
                    else:
                        await response.aread()
            else:
                response = await client.post(_ENDPOINT, headers=headers, json=payload)
        except httpx.TimeoutException:
            # A timeout is its own BF-19 category (Orchestrator._handle_backend_timeout)
            # — not folded into the transient-retry loop above, since a request that
            # already burned a full `self._timeout` is expensive to repeat blindly.
            raise AgentTimeout(
                f"OpenCode Zen API timed out after {self._timeout}s"
            ) from None
        except httpx.HTTPError as exc:
            # Anything else httpx can raise on the connect/write/read path (ConnectError,
            # ReadError, RemoteProtocolError, PoolTimeout, ...) is a transport-level
            # failure, not a "the request completed and the response was bad" failure —
            # treat it the same as a gateway 5xx: retry this backend a couple of times
            # (_call_api's loop) before BF-19 gives up on it. Without this, any of these
            # exceptions propagated raw past all three typed BF-19 exceptions and hard-
            # failed the job on the very first configured backend.
            raise _TransientOpenCodeError(
                f"OpenCode Zen API transport error: {exc}"
            ) from None
        finally:
            await client.aclose()

        if response.status_code == 429:
            raise AgentLimitReached(f"OpenCode Zen API rate limit reached: {response.text[:500]}")

        if streamed_content:
            return streamed_content

        if streamed_content == "":
            # HTTP 200, streamed, but no delta content was extracted. Before
            # assuming a generic transient/empty-stream failure, try to recover
            # the raw stream body as a JSON error envelope -- this backend is
            # documented to sometimes return a 200-status response whose body is
            # a single JSON error object (e.g. a transient upstream 502 surfaced
            # as {"error": {"type": "server_error"}} at HTTP 200) rather than a
            # real SSE stream, which _consume_sse's "data:"-only parsing would
            # otherwise silently drop.
            try:
                body = json.loads(streamed_raw) if streamed_raw else None
            except ValueError:
                body = None
            if not isinstance(body, dict) or "error" not in body:
                raise _TransientOpenCodeError(
                    "OpenCode Zen API stream produced no content (model may have "
                    "produced only reasoning tokens before hitting max_tokens)"
                )
            # Fall through to the shared "error" in body classification below,
            # using the body recovered from the stream instead of response.json().
        else:
            # A gateway-level failure (e.g. a 502/503 from the proxy in front of the
            # API, as opposed to the API's own JSON error envelope) can return
            # non-JSON — HTML, plain text. Check that *before* touching
            # status_code>=400 below, since that branch depends on `body` and must
            # never be reached via a JSONDecodeError.
            try:
                body = response.json()
            except ValueError:
                detail = f"OpenCode Zen API error {response.status_code}: {response.text[:500]}"
                if response.status_code >= 500:
                    raise _TransientOpenCodeError(detail) from None
                raise _permanent_4xx(detail, tools_present=tools is not None) from None

        if "error" in body:
            err = body["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if "rate" in message.lower() or "credit" in message.lower() or err_type in {"RateLimitError", "CreditsError"}:
                raise AgentLimitReached(f"OpenCode Zen API limit reached: {message}")
            # A structured error body at a 4xx status is a permanent config/client
            # problem (bad model name, malformed request, auth failure) — retrying
            # this backend won't help. Everything else — a 200 with an error
            # envelope (the live-observed transient-upstream-502 shape, `{"error":
            # {"type": "server_error"}}` at status 200) or a 5xx — is treated as
            # transient/retryable by default: the only *confirmed* permanent shape
            # is a real 4xx, and guessing at every possible upstream error `type`
            # string for "this one's actually transient too" risks silently
            # treating an unrecognized transient shape as permanent instead.
            if 400 <= response.status_code < 500:
                raise _permanent_4xx(
                    f"OpenCode Zen API error: {message}", tools_present=tools is not None
                )
            raise _TransientOpenCodeError(f"OpenCode Zen API error: {message}")

        if response.status_code >= 400:
            detail = f"OpenCode Zen API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise _TransientOpenCodeError(detail)
            raise _permanent_4xx(detail, tools_present=tools is not None)

        choices = body.get("choices")
        if not choices:
            # A 200 with a well-formed JSON body but an empty/missing `choices` list (a
            # proxy bug, or a model that produced zero completions) is not covered by the
            # "error" key check above. Treat it the same as a null message content below —
            # transient and worth retrying on this backend before BF-19 gives up on it,
            # rather than letting an unhandled IndexError/KeyError escape past all three
            # typed BF-19 exceptions and hard-fail the job on the first backend.
            raise _TransientOpenCodeError(
                f"OpenCode Zen API returned no choices: {response.text[:500]}"
            )
        message = choices[0].get("message") or {}
        if tools is not None:
            # Extracted BEFORE the null-content check below, deliberately: a tool-call
            # reply legitimately carries `content: null` on this wire shape, and that
            # check would otherwise classify a perfectly good tool call as a transient
            # null-content failure and burn all _MAX_ATTEMPTS retries on it. Gated on
            # `tools is not None` so a non-tool turn's classification is untouched;
            # tools sent but no calls returned still falls through (a tools-present
            # null content with no calls is a genuine transient failure and must still
            # retry).
            wire_calls = message.get("tool_calls")
            if isinstance(wire_calls, list) and wire_calls:
                return wire_calls
        content = message.get("content")
        if content is None:
            # Reasoning models can return a null content when the reply is all
            # `reasoning` (e.g. truncated by max_tokens before any final answer).
            # Retried on the chance this is a per-call sampling artifact rather
            # than a deterministic max_tokens overflow — cheaper to try again
            # than to assume the worst, though unlike the 5xx/gateway cases each
            # retry here costs a full generation, not a fast-failing round trip.
            raise _TransientOpenCodeError(
                "OpenCode Zen API returned null message content (model may have "
                "produced only reasoning tokens before hitting max_tokens)"
            )
        return content
