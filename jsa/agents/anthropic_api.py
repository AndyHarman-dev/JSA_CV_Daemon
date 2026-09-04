"""AnthropicAPIBackend: REST API implementation of AgentBackend using the Anthropic SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

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
from jsa.agents.tool_spec import ToolSpec, to_anthropic_tools
from jsa.schema.turn_models import parse_structured_reply_for_schema

logger = logging.getLogger(__name__)

# The single tool every structured-mode request offers. Its name is arbitrary — the
# model never gets to choose it as a "real" tool (tool_choice forces it) — it exists
# so the reply lands as a tool_use block whose input is schema-constrained JSON
# instead of free text. Forced tool-use (not the SDK's native output_config
# structured outputs) because output_config mandates additionalProperties: false
# recursively on every nested object, which the turn models' nested $defs
# (CVDocument/CoverLetter, extra="ignore" with defaulted optionals) do not satisfy;
# a tool's input_schema has no such recursive-strictness requirement.
_TOOL_NAME = "respond"


class _ToolsRejected(ToolsUnsupported):
    """Internal only: an ``anthropic.BadRequestError`` raised while NATIVE tool
    definitions (``jsa/agents/tool_spec.py``'s revision vocabulary) were actually in
    the payload — never for the structured-mode ``respond`` forced tool, which sends
    ``tools``/``tool_choice`` too but has no fallback rung to catch this.

    **Subclasses ``ToolsUnsupported``, NOT ``AgentBackendUnavailable``**, and gets no
    in-backend retry-clean — deliberately breaking the ``_CacheRejected`` mould the
    revision-tool-use plan's Phase 3 preamble sketched. The canonical statement of this
    reasoning lives on ``_openai_compat.py``'s ``_ToolsRejected``; restated here for
    this backend:

    1. Prompt caching is an optional enrichment with NO functional fallback, so it must
       shed in-backend and retry clean (on this backend it doesn't even do that — see
       CLAUDE.md "Anthropic: no degrade path needed"). Tool mode DOES have a fallback,
       owned by a different layer: ``jsa/pipeline/tool_loop.py``'s native -> prompt rung
       ladder, which rebuilds the session with a tool-contract system prompt and no wire
       tools at all. Retrying clean in-process would instead send a tool-contract system
       prompt with no tools attached and hand the resulting sentinel/structured reply
       back to the loop — semantically incoherent.
    2. If this subclassed ``AgentBackendUnavailable``, an escaped instance would tell
       BF-19 to advance the whole job to the next configured backend over a tools-only
       degrade — exactly the loss the rung ladder exists to prevent. See
       ``ToolsUnsupported``'s docstring in ``jsa/agents/base.py``.

    So this propagates out of the backend untouched; ``tool_loop.py`` catches
    ``ToolsUnsupported`` around both its ``restore_session``+``send_message`` entry and
    its ``send_tool_results`` follow-ups.
    """


@dataclass(kw_only=True)
class AnthropicSessionHandle(SessionHandle):
    """Session handle for AnthropicAPIBackend; carries conversation history in memory."""
    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can pass it
    messages: list[dict] = field(default_factory=list)  # growing conversation list
    # The session's structured-output mode, established once at start/restore time.
    # Carried on the handle so follow-up send_message calls stay in the same mode
    # without re-passing the schema (capability ≠ per-call choice: a session is
    # either structured or sentinel for its whole life — no anthropic downgrade).
    structured_schema: dict[str, Any] | None = None
    # The session's NATIVE tool vocabulary, established at start/restore time by
    # tool_loop.py's native rung. Mutually exclusive with structured_schema (the
    # terminal tool's arguments ARE this turn's structured output) — enforced at
    # both session-establishment call sites and in _tool_kwargs.
    tools: tuple[ToolSpec, ...] | None = None


def _tool_kwargs(
    *,
    schema: dict[str, Any] | None = None,
    tools: tuple[ToolSpec, ...] | None = None,
) -> dict[str, Any]:
    """Request kwargs for whichever tool channel this request uses, or ``{}`` for neither.

    Two mutually exclusive shapes:

    * ``schema`` — structured mode's single forced ``respond`` tool. Request-building
      half of the thin forced-tool adapter; a future swap to the SDK's native
      response_format/output_config mechanism is contained inside this function and
      ``_extract_structured_text`` (the extraction half). Byte-identical to the
      pre-native-tools ``_forced_tool_kwargs``.
    * ``tools`` — the native revision vocabulary, with ``tool_choice {"type": "any"}``
      (the model must call SOME tool, but picks which — unlike structured mode's
      single forced name).

    Passing both is a programming error: tool mode sends no structured schema because
    the terminal tool's arguments are the structured output.
    """
    if schema is not None and tools:
        raise ValueError(
            "structured_schema and native tools are mutually exclusive per request — "
            "in tool mode the terminal tool's arguments are the structured output"
        )
    if schema is not None:
        return {
            "tools": [{"name": _TOOL_NAME, "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
        }
    if tools:
        return {
            "tools": to_anthropic_tools(tools),
            "tool_choice": {"type": "any"},
        }
    return {}


def _extract_structured_text(response: Any) -> str:
    """Extraction half of the forced-tool adapter: canonical JSON text from a response.

    Checks ``stop_reason`` BEFORE extracting — a ``max_tokens`` stop means the tool
    input was cut off mid-stream, so whatever partial ``input`` the SDK managed to
    parse must never be treated as a complete reply (that truncation is a
    ProtocolError variant that Phase 5 routes into the self-heal budget: "output
    truncated; re-emit more concisely"). Then scans the content blocks for the
    ``tool_use`` block rather than indexing ``content[0]`` — a model under forced
    tool-use may still emit text blocks before the tool call.

    Returns the canonical union-JSON plain text (``json.dumps`` of the tool input),
    never the provider envelope — the canonical-form invariant (see
    jsa/schema/turn_models.py's module docstring) starts here. ``ensure_ascii=False``
    so the DB Message rows this text lands in (stages.py persists ``reply.raw``)
    stay human-readable UTF-8 for non-English CVs/letters, mirroring the sentinel
    path's naturally-unescaped raw model text; every consumer json.loads's it, so
    the escaping choice is semantically irrelevant (advisor-verified).
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ProtocolError(
            "structured reply truncated: stop_reason == 'max_tokens' "
            "(output cut off before the forced tool call completed)"
        )
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "tool_use":
            return json.dumps(block.input, ensure_ascii=False)
    raise ProtocolError(
        f"structured reply unparseable: no tool_use block in the response "
        f"(stop_reason={stop_reason!r})"
    )


def _extract_tool_calls(response: Any) -> list[ToolCall]:
    """Every ``tool_use`` block in the response, in document order.

    Deliberately NOT ``_extract_structured_text``'s "first tool_use block wins" scan:
    structured mode forces exactly one ``respond`` call, but a native tool turn can
    legitimately carry several calls in one assistant message (the loop executes them
    in array order — see ``tool_loop.py``'s stop-at-the-first-terminal rule), so
    dropping all but the first would silently discard work.

    Keeps ``_extract_structured_text``'s ``stop_reason == "max_tokens"`` guard for the
    same reason: a truncated turn's partially-parsed tool ``input`` is not a complete
    call and must never be executed as one.

    Returns ``[]`` (never raises) when the model answered with no tool_use block at
    all — that is the loop's ``_no_parseable_call`` case, not a protocol violation.
    ``block.id`` is the provider-issued call id, which the ``tool_result`` blocks in
    ``send_tool_results`` must echo back verbatim; only the prompt rung synthesizes
    ``call_0``/``call_1`` ids (see ``ToolCall``'s docstring).
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ProtocolError(
            "tool-mode reply truncated: stop_reason == 'max_tokens' "
            "(output cut off before the tool call(s) completed)"
        )
    calls: list[ToolCall] = []
    for index, block in enumerate(getattr(response, "content", [])):
        if getattr(block, "type", None) != "tool_use":
            continue
        arguments = getattr(block, "input", None)
        calls.append(
            ToolCall(
                # The API always issues an id on a tool_use block; the fallback is
                # defence only, and is keyed on the BLOCK's position (not the count of
                # calls collected so far) so two id-less blocks can never collide.
                # A duplicate id would make the next turn's tool_result blocks
                # ambiguous — a 400 that would surface as an unexplained rung
                # downgrade rather than as the bug it is.
                id=str(getattr(block, "id", "") or f"call_{index}"),
                name=str(getattr(block, "name", "")),
                # ToolCall.arguments is contractually a plain dict — a provider that
                # somehow hands back a non-object input becomes an argument-validation
                # failure in the applier (an ordinary tool result, D5), not a crash.
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return calls


def _response_text(response: Any) -> str:
    """The response's ``text`` blocks joined, ignoring every other block type.

    Filtering on ``type == "text"`` first matters: a ``tool_use`` block has no text,
    and reading ``.text`` off one would either fail or (against a test double) invent
    a value.
    """
    parts: list[str] = []
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _reject_both_modes(
    schema: dict[str, Any] | None, tools: tuple[ToolSpec, ...] | None
) -> None:
    """Guard the tool-mode/structured-mode mutual exclusion at session establishment.

    Raised here rather than only inside ``_tool_kwargs`` so the error names the caller's
    mistake at the point it was made, instead of surfacing one round-trip later.
    """
    if schema is not None and tools:
        raise ValueError(
            "AnthropicAPIBackend: a session is either structured or native-tool mode, "
            "never both — in tool mode the terminal tool's arguments are the "
            "structured output"
        )


def _parse_reply_for(raw: str, schema: dict[str, Any] | None) -> AgentReply:
    """Parse a reply per session mode: sentinel grammar, or the structured union-JSON."""
    if schema is None:
        return parse_reply(raw)
    return parse_structured_reply_for_schema(raw, schema)


class AnthropicAPIBackend(AgentBackend):
    """AgentBackend implementation that calls the Anthropic messages API directly.

    Structured mode (``structured_schema`` passed at session start) forces the
    model to reply through a single tool whose ``input_schema`` is the stage's
    turn-union schema; sentinel mode (no schema) is exactly the pre-structured
    behavior. ``supports_structured_output`` is True — the wire protocol can
    enforce a JSON schema on the reply (CLAUDE.md → backend fallback chain /
    structured-output plan "Locked decisions" #1).

    Native tool mode (``tools`` passed at session start/restore) is the third,
    mutually exclusive channel — ``jsa/pipeline/tool_loop.py``'s rung 1 for revision
    patching. It sends the revision vocabulary with ``tool_choice {"type": "any"}``
    and no structured schema, returns ``kind="tool_calls"`` replies, and continues
    through ``send_tool_results``. A tools-present 400 becomes ``_ToolsRejected``
    (a ``ToolsUnsupported``), which downgrades that turn to the prompt rung instead
    of costing the job its BF-19 slot.
    """

    name = "anthropic"
    supports_structured_output = True
    # SDK messages.stream() for content only. Structured mode (forced tool-use)
    # streams nothing — the tool_use input JSON is not useful to show
    # token-by-token, and _extract_structured_text needs the complete block.
    supports_streaming = True
    # Native tool calling (jsa/pipeline/tool_loop.py's rung 1). A plain ClassVar is
    # correct here — unlike OpenCodeGoBackend, this backend speaks exactly one wire
    # protocol, so there is no per-instance split to read off the instance.
    supports_native_tools = True

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        timeout: float = 180.0,
        prompt_caching: bool = True,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._prompt_caching = prompt_caching

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> tuple[AnthropicSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply.

        ``tools`` is accepted for the ``supports_native_tools`` contract in
        ``base.py``, but no production caller uses it here: revisions always
        ``restore_session`` (see ``tool_loop.py``'s module docstring).
        """
        _reject_both_modes(structured_schema, tools)
        handle = AnthropicSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=[],
            structured_schema=structured_schema,
            tools=tools,
        )
        if tools:
            # Tool mode never streams (see _tool_round) — on_chunk is dropped here
            # by construction, not by omission.
            reply = await self._tool_round(
                handle, [{"role": "user", "content": initial_user_msg}]
            )
            return handle, reply
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        # on_retry is accepted-but-unused here per base.py's OnRetry contract:
        # this backend has no in-flight replay-and-retry shape of its own (no
        # sentinel-nudge loop, no in-backend transient-HTTP retry loop).
        raw = await self._call_api(system_prompt, messages, structured_schema, on_chunk)
        reply = _parse_reply_for(raw, structured_schema)
        messages.append({"role": "assistant", "content": raw})
        handle.messages = messages
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> AnthropicSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        The session's structured mode (or, with ``tools``, its native tool mode) is
        (re)established here from the caller's arguments, mirroring start_session.
        This is the entry point ``tool_loop.py``'s native rung uses.
        """
        _reject_both_modes(structured_schema, tools)
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = AnthropicSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
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
        effective schema is the explicit kwarg if given, else the one the handle
        carries from start/restore time — omitting it keeps the session's mode.
        """
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        if handle.tools:
            # Mutual exclusivity, asserted at the real call site and not only inside
            # _tool_kwargs: a caller that hands a schema to a tool-mode session has a
            # bug, and silently preferring one over the other would hide it.
            _reject_both_modes(structured_schema, handle.tools)
            # on_chunk/on_retry are dropped deliberately: tool mode never streams
            # (plan Phase 2 finding #1 — the tool_use input JSON is not renderable
            # token-by-token and _extract_tool_calls needs whole blocks). Do not
            # "fix" this by threading on_chunk through the tool path.
            return await self._tool_round(handle, [{"role": "user", "content": text}])
        schema = structured_schema if structured_schema is not None else handle.structured_schema
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages, schema, on_chunk)
        reply = _parse_reply_for(raw, schema)
        # Mutate only after success so handle stays consistent on error
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": raw})
        return reply

    async def send_tool_results(
        self, handle: SessionHandle, results: list[ToolResult]
    ) -> AgentReply:
        """Continue a native tool session with the outcomes of the last round of calls.

        Sends one user turn of ``tool_result`` blocks — one per call, echoing the
        provider-issued ``tool_use_id`` the Messages API requires — with the tool
        definitions still attached, and returns the model's next reply (another
        ``tool_calls`` round, or the no-tool_use fallback described in
        ``_tool_round``). The assistant turn carrying the matching ``tool_use``
        blocks was already appended by the call that produced these results, so it is
        not re-added here.
        """
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        if not handle.tools:
            # A caller bug, not a provider rejection. Without this the request would
            # go out with tool_result blocks and no tools attached, 400, and be
            # classified as _ToolsRejected — misattributing the caller's mistake to a
            # provider tools rejection and quietly downgrading the rung.
            raise ValueError(
                "send_tool_results requires a native tool session — this handle has "
                "no tools (was it restored without tools=?)"
            )
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                # json.dumps, not the raw dict: the API wants text (or a content-block
                # list) here, and the applier's outcome dicts are plain JSON. ok/error
                # is inside `content` already, so no `is_error` flag is set — the
                # model reads the error envelope the applier produced verbatim.
                "content": json.dumps(result.content, ensure_ascii=False),
            }
            for result in results
        ]
        return await self._tool_round(handle, [{"role": "user", "content": blocks}])

    async def _tool_round(
        self, handle: AnthropicSessionHandle, new_turns: list[dict]
    ) -> AgentReply:
        """One native tool-mode round-trip: append ``new_turns``, call, return the reply.

        Mirrors ``send_message``'s discipline — ``handle.messages`` is mutated only
        after a successful API call, so a timeout/rejection leaves the conversation
        coherent for a retry on another rung.

        A reply carrying tool_use blocks becomes ``kind="tool_calls"``; its ``raw``/
        ``content`` are the response's plain text blocks (usually ``""``), never a
        serialization of the wire envelope — the canonical-form invariant (CLAUDE.md)
        means a provider ``tool_use`` block must never reach a ``Message`` row.

        A reply with NO tool_use block is returned as an ordinary non-tool_calls reply
        rather than raising. It is deliberately not run through ``parse_reply``: that
        raises ``ProtocolError`` on sentinel-less prose, and ``tool_loop.py`` catches
        only ``ToolsUnsupported``, so such an error would escape into
        ``Orchestrator._run_one``'s generic handler and hard-fail the job. The loop
        only ever tests ``kind != "tool_calls"`` (``_no_parseable_call`` on entry,
        the ``while`` condition mid-loop), so this reply is contained: it triggers the
        native -> prompt downgrade or the fall-through to rung 3, and its content is
        never treated as a finished document.
        """
        pending = handle.messages + new_turns
        calls, text = await self._call_api_tools(
            handle.system_prompt, pending, handle.tools or ()
        )
        handle.messages.extend(new_turns)
        if calls:
            handle.messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                        for call in calls
                    ],
                }
            )
            return AgentReply(raw=text, content=text, kind="tool_calls", tool_calls=calls)
        handle.messages.append({"role": "assistant", "content": text})
        return AgentReply(raw=text, content=text, kind="final")

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op for stateless REST API — just clear in-memory history."""
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
    ) -> str:
        """Call the Anthropic messages API and return the raw reply text.

        Sentinel mode (``structured_schema is None``) is otherwise the
        pre-structured behavior: no tools kwargs, first content block's text
        (``system`` becomes a cache-annotated block array when
        ``self._prompt_caching`` is on — see below — but that's a shape change,
        not a behavior change). Structured mode adds the forced-tool kwargs and
        extracts the canonical JSON from the tool_use block (see the two
        adapter functions above).

        Deliberately still ``-> str``: the rest of the module treats a reply as raw
        text, so native tool mode gets its own ``_call_api_tools`` beside this rather
        than a widened return type (revision-tool-use plan, E1 detail).
        """
        response = await self._request(
            system_prompt,
            messages,
            extra_kwargs=_tool_kwargs(schema=structured_schema),
            native_tools=False,
            # Structured mode has never streamed — the tool_use input JSON isn't
            # renderable token-by-token and _extract_structured_text needs the
            # complete block.
            on_chunk=on_chunk if structured_schema is None else None,
        )
        if structured_schema is None:
            return response.content[0].text
        return _extract_structured_text(response)

    async def _call_api_tools(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: tuple[ToolSpec, ...],
    ) -> tuple[list[ToolCall], str]:
        """Native tool-mode call: the model's tool calls plus any accompanying text.

        The plan sketches this as ``-> list[ToolCall]``; it returns the response's
        text blocks alongside them because ``_tool_round`` needs that text to build
        the no-tool_use fallback reply. The point of the plan's note is honored:
        ``_call_api``'s ``-> str`` is untouched.

        Never streams — no ``on_chunk`` is accepted, let alone forwarded (see
        ``_tool_round``).
        """
        response = await self._request(
            system_prompt,
            messages,
            extra_kwargs=_tool_kwargs(tools=tools),
            native_tools=True,
            on_chunk=None,
        )
        return _extract_tool_calls(response), _response_text(response)

    async def _request(
        self,
        system_prompt: str,
        messages: list[dict],
        *,
        extra_kwargs: dict[str, Any],
        native_tools: bool,
        on_chunk: OnChunk | None,
    ) -> Any:
        """Issue one Messages API call and return the SDK response object.

        Shared transport for every mode — sentinel, structured, and native tool —
        so timeout/quota/bad-request classification and cache-usage logging can
        never drift between them.

        ``native_tools`` says whether ``extra_kwargs`` carries THIS FEATURE's tool
        definitions, and is passed by the caller rather than sniffed out of the
        payload. That distinction is load-bearing: structured mode also sends
        ``tools``/``tool_choice`` (the forced ``respond`` tool), but a structured-mode
        rejection has no rung ladder to catch a ``ToolsUnsupported`` — it must stay a
        plain ``AgentBackendUnavailable`` so BF-19 engages. Do not replace this flag
        with an ``"tools" in create_kwargs`` check.

        The client is created per-call and explicitly closed in a finally block
        so the httpx connection pool is released on both normal exit and
        timeout cancellation.

        Unlike ClaudeCliBackend/GoogleCliBackend (see jsa/agents/_subprocess.py),
        this backend has no killable-subprocess problem to solve: `await
        client.messages.create(...)` is a real async operation, so
        Orchestrator.cancel_task()'s task.cancel() can interrupt it directly at
        this await point — no process to leak, nothing to kill.
        """
        import anthropic

        system: str | list[dict[str, Any]] = system_prompt
        if self._prompt_caching:
            # Default 5-minute TTL (no `ttl` key) — see CLAUDE.md "Prompt caching"
            # -> Phase 6: job-to-job reuse under continuous dispatch pays off well
            # inside a 5-minute start-to-start gap, so the 1-hour TTL's 2x write
            # premium buys nothing here. Tools render before system (CLAUDE.md's
            # documented render order), so this one breakpoint covers the tool
            # definitions too — structured mode's forced `respond` schema and native
            # tool mode's whole vocabulary alike. No second cache_control on the tool
            # definitions is needed, and adding one would be a wire change for
            # nothing.
            system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 32000,
            "system": system,
            "messages": messages,
        }
        create_kwargs.update(extra_kwargs)

        use_stream = on_chunk is not None
        client = anthropic.AsyncAnthropic()
        try:
            if use_stream:
                response = await asyncio.wait_for(
                    self._stream_and_collect(client, create_kwargs, on_chunk),
                    timeout=self._timeout,
                )
            else:
                response = await asyncio.wait_for(
                    client.messages.create(**create_kwargs),
                    timeout=self._timeout,
                )
        except asyncio.TimeoutError:
            raise AgentTimeout(
                f"Anthropic API timed out after {self._timeout}s"
            ) from None
        except anthropic.RateLimitError as exc:
            raise AgentLimitReached(
                f"Anthropic API rate limit reached: {exc}"
            ) from exc
        except anthropic.BadRequestError as exc:
            # A 400 while native tool definitions were attached is (most likely) the
            # model/config not accepting this tool payload — a tools-only failure with
            # a real fallback, so it becomes _ToolsRejected and tool_loop.py downgrades
            # THIS TURN to the prompt rung on the SAME backend. Every other 400 is a
            # genuine bad request that retrying this backend won't fix: raise
            # AgentBackendUnavailable so BF-19's chain advances instead of the job
            # hard-failing in Orchestrator._run_one's generic `except Exception`
            # (which is what an un-caught BadRequestError did before this).
            #
            # Deliberately NOT widened to anthropic.APIStatusError: an
            # AuthenticationError (401) or NotFoundError (404 — bad model id) still
            # propagates raw and hard-fails the job. That is a real adjacent hole, and
            # closing it here would be scope creep beyond this feature (revision-tool-
            # use plan, E1 detail — "flag it, don't do it").
            if native_tools:
                raise _ToolsRejected(
                    f"Anthropic API rejected the native tool payload: {exc}"
                ) from exc
            raise AgentBackendUnavailable(
                f"Anthropic API rejected the request: {exc}"
            ) from exc
        finally:
            await client.close()

        _log_cache_usage(response)
        return response

    async def _stream_and_collect(
        self, client: Any, create_kwargs: dict[str, Any], on_chunk: OnChunk
    ) -> Any:
        """Stream content deltas through ``on_chunk`` via the SDK's
        ``messages.stream()`` helper, then return the final assembled
        ``Message`` object — same shape ``client.messages.create`` returns, so
        the caller's post-processing (``_log_cache_usage``,
        ``response.content[0].text``) is unchanged."""
        async with client.messages.stream(**create_kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    await on_chunk(AgentChunk(kind="content", text=text))
            return await stream.get_final_message()


def _log_cache_usage(response: Any) -> None:
    """Log cache_read/cache_creation token counts, isinstance-guarded.

    ``response.usage`` is normally a real SDK ``Usage`` object, but a test
    double or an SDK change could hand back something else entirely — guard
    with isinstance, not just attribute presence, so a wrong-typed usage
    object never raises past an otherwise-successful reply.
    """
    usage = getattr(response, "usage", None)
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)
    if isinstance(cache_read, int) or isinstance(cache_creation, int):
        logger.info(
            "anthropic API cache_read_input_tokens=%s cache_creation_input_tokens=%s",
            cache_read,
            cache_creation,
        )
