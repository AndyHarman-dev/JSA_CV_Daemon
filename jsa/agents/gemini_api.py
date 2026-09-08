"""GeminiBackend: raw-httpx REST backend for Google's native Gemini API.

``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent``
— NOT the OpenAI-compat shim ``google-cli`` (the ``agy`` CLI) uses, and NOT the SDK
(``google-genai`` is deliberately not a new dependency — the rest of this project is
SDK-free except ``anthropic``; see the multi-backend-model-select plan's locked
decisions).

Session/nudge/downgrade/retry orchestration is inherited unchanged from
``jsa.agents._openai_compat.OpenAICompatBackend`` — only the network call
(``_call_api_once``) is overridden, since Gemini's wire shape (``contents``/``parts``,
a top-level ``systemInstruction``, ``x-goog-api-key`` auth) has nothing in common with
an OpenAI-compatible ``/chat/completions`` body. This mirrors how
``OpenCodeGoBackend``'s ``/messages`` protocol path reuses ``retry_transient``/
``TransientBackendError`` without inheriting the ``/chat/completions`` payload
builder — the shared base's session machinery is protocol-agnostic on purpose.

Auth: ``x-goog-api-key``, ``GEMINI_API_KEY`` falling back to ``GOOGLE_API_KEY``, read
at call time via the inherited ``_api_key()`` (its ``env_vars`` fallback-chain
mechanism already does exactly this). The header form is what the plan specifies;
the Phase-0 probe script only exercised the ``?key=`` query-string form (both are
Google-documented) — see the Change Log for that provenance distinction.

``contents`` role normalization: Gemini requires ``contents`` to start with a
``"user"`` entry and never carry two adjacent same-role entries.
``_to_gemini_contents`` merges any adjacent same-role turns into one content entry
with multiple ``parts`` rather than emitting an invalid array — this only matters
on a ``restore_session`` replay (fresh ``start_session``/``send_message`` traffic
already alternates user/assistant by construction), but a resumed or BF-19-switched
session's history is not guaranteed to preserve strict alternation once
``adapt_history`` has rewritten row content (never role) for the destination mode.

Structured mode sends ``generationConfig.responseSchema`` with the JSON schema
inlined via ``jsa.schema.turn_models.inline_defs`` — see that function's docstring
for why (Gemini's schema field rejects ``$defs``/``$ref``/``additionalProperties``,
confirmed live against three current Gemini models, Phase 0 probe #4).

**Schema rejection must downgrade, never hard-fail the chain** (the plan's Phase 3
"Critical" line). A permanent 4xx from ``_call_api_once`` while ``structured_schema
is not None`` is raised as the module-private ``_SchemaRejected`` (a subclass of
``AgentBackendUnavailable``, so an unhandled instance still degrades correctly)
instead of ``AgentBackendUnavailable`` directly. ``start_session``/``send_message``
below catch it and retry the SAME call once with structured mode turned off,
landing the session in exactly the state an unparseable-2xx-reply downgrade would
(``handle.structured_enabled = False``) — this covers a schema the API rejects
outright, not just a schema it accepts but the model ignores (the inherited
downgrade already covered that case). If the retried sentinel-mode call also fails,
its exception (a real ``AgentBackendUnavailable``/``AgentLimitReached``/
``AgentTimeout``) propagates normally into BF-19.

``finishReason == "MAX_TOKENS"`` raises ``ProtocolError("structured reply
truncated")``, mirroring ``anthropic_api.py``'s ``_extract_structured_text`` — but
checked unconditionally (both structured AND sentinel mode), not gated on
``structured_schema is not None`` the way Anthropic's is. Anthropic's asymmetry (no
truncation check in its sentinel path) is pre-existing behavior this module does not
need to reproduce: Gemini's extraction has one code path regardless of mode, and a
truncated sentinel reply is just as broken as a truncated structured one — both should
spend the self-heal budget rather than being handed to the parser as if complete.
``generationConfig.maxOutputTokens`` is pinned to 32000 (matching every other
backend's ``max_tokens``) precisely so this check fires against a limit this project
chose, not whatever Gemini's un-set default happens to be.

**Native tool calling** (revision-tool-use plan, E4) sends ``tools:
[{"functionDeclarations": [...]}]`` plus ``toolConfig.functionCallingConfig.mode =
"ANY"`` (this API's equivalent of ``tool_choice: "required"``), and NEVER a
``responseSchema`` alongside them — tool mode and structured mode are mutually
exclusive per request, because the terminal tool's arguments ARE the structured
output. Three things about it are load-bearing:

* ``functionCall`` parts are extracted BEFORE the ``if not text`` transient raise at
  the bottom of ``_call_api_once`` (the plan's finding #3): a functionCall-only reply
  carries no text part at all, so that raise would otherwise misclassify a working
  tool turn as an empty reply, burn every retry attempt, and end as
  ``AgentBackendUnavailable`` — dropping this backend out of BF-19 over a good call.
* The extracted calls are normalized into the OpenAI-compatible ``tool_calls`` entry
  shape (``_function_calls_to_tool_calls``) so the inherited ``start_session``/
  ``send_message``/``_parse_tool_calls`` machinery applies unchanged; only
  ``send_tool_results`` is overridden, converting that normalized array back into
  Gemini's ``functionCall``/``functionResponse`` parts.
* A permanent 4xx while declarations were actually on the wire raises
  ``_openai_compat._ToolsRejected`` — a ``ToolsUnsupported``, NOT an
  ``AgentBackendUnavailable`` — and it PROPAGATES. There is deliberately no
  in-backend degrade-and-retry for tools the way there is for reasoning/caching:
  ``jsa/pipeline/tool_loop.py`` owns the native -> prompt rung ladder. See
  ``_ToolsRejected``'s docstring in ``_openai_compat.py`` and ``ToolsUnsupported``'s
  in ``jsa/agents/base.py``.

``generationConfig.thinkingConfig.thinkingBudget`` is pinned (``_THINKING_BUDGET``),
never left dynamic — see ``GeminiBackend._reasoning_payload``'s docstring. Gemini
counts thought tokens against this same ``maxOutputTokens`` ceiling, and an unbounded
thought trace was observed live starving the reply down to a schema-valid-but-thin CV
(a Summary section only, no Experience content) rather than tripping the MAX_TOKENS
check above.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from jsa.agents._openai_compat import (
    OpenAICompatBackend,
    OpenAICompatSessionHandle,
    TransientBackendError,
    _parse_tool_calls,
    _ReasoningRejected,
    _text_reply_in_tool_mode,
    _tool_calls_reply,
    _ToolsRejected,
)
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentChunk,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    OnChunk,
    OnRetry,
    SessionHandle,
    ToolResult,
)
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import ToolSpec, to_gemini_function_declarations
from jsa.schema.turn_models import inline_defs

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_MAX_OUTPUT_TOKENS = 32000
# Thinking tokens count against _MAX_OUTPUT_TOKENS on Gemini (see _reasoning_payload's
# docstring) — this caps the model's thought trace so a guaranteed floor of the ceiling
# stays reserved for the actual reply content.
_THINKING_BUDGET = 8192


class _SchemaRejected(AgentBackendUnavailable):
    """Internal only: a permanent 4xx from a structured-mode call, raised instead of
    plain ``AgentBackendUnavailable`` so ``start_session``/``send_message`` can catch
    it and downgrade to sentinel mode rather than dropping Gemini out of BF-19. See
    the module docstring's "Schema rejection must downgrade" note. Subclasses
    ``AgentBackendUnavailable`` as a safety net: an instance that somehow escapes
    both overrides still classifies correctly for BF-19."""


# The tools rejection is imported, NOT redefined: GeminiBackend is a real subclass of
# OpenAICompatBackend (it already imports ``_ReasoningRejected`` from there), so the
# canonical ``_ToolsRejected`` applies verbatim. This is the opposite of
# ``opencode_zen.py``, which keeps its own independent copy under CLAUDE.md's
# no-shared-base rule for that module — that rule is about a module that does not
# inherit from this base, and does not apply here.


def _to_gemini_role(role: str) -> str:
    """Gemini's ``contents[].role`` is ``"user"`` or ``"model"`` — never
    ``"assistant"``, which is how every other backend's session handle stores it.

    This is also the ONLY role mapper a tool-mode turn passes through, so it settles
    what role a function-response turn takes: **``"user"``**. The v1beta
    ``generateContent`` API admits exactly two role values in ``contents``, ``user``
    and ``model`` — there is no ``"tool"``/``"function"`` role the way the
    OpenAI-compatible wire shape has — so one round's results are simply the user's
    next turn, carrying ``functionResponse`` parts instead of text (and the model's
    request for them is a ``model`` turn carrying ``functionCall`` parts). Nothing in
    ``send_tool_results`` needs a third mapping; ``"assistant"``/anything-else already
    lands on the right side of this function.
    """
    return "model" if role == "assistant" else "user"


def _gemini_parts(message: dict) -> list[dict]:
    """The ``parts`` array for one internal message entry.

    A text turn carries ``{"role", "content"}`` — the shape every other backend on
    this base uses, and the only shape ``restore_session``'s replayed history ever
    produces. A tool-mode turn written by ``send_tool_results`` carries a pre-built
    ``parts`` list of ``functionCall``/``functionResponse`` blocks instead, because
    neither has any text representation on this wire shape.
    """
    parts = message.get("parts")
    if parts is not None:
        return list(parts)
    return [{"text": message["content"]}]


def _as_response_object(content: Any) -> dict[str, Any]:
    """``functionResponse.response`` must be a JSON **object** on this wire shape.

    The applier's outcomes (``jsa/schema/patch.py``) already are dicts, so the wrap is
    defence only — but a bare scalar or array would be rejected by the API as a
    malformed request, which ``_permanent_4xx`` would then report as a tools rejection
    and quietly cost the turn its native rung. Misattributing our own malformed
    payload to the provider is exactly the failure mode worth one ``isinstance``.
    """
    return content if isinstance(content, dict) else {"result": content}


def _function_calls_to_tool_calls(parts: list[Any]) -> list[dict]:
    """Gemini ``functionCall`` parts -> the OpenAI-compatible ``tool_calls`` entry shape.

    Normalized rather than returned raw so that everything the shared base already
    does with ``_call_api_once``'s list return value — ``_parse_tool_calls``,
    ``_tool_calls_reply``, parking the array on ``handle.pending_tool_calls`` — applies
    to this backend unchanged, leaving ``send_tool_results`` as the only session method
    this module has to override. The conversion is lossless (a ``functionCall`` carries
    nothing but ``name`` and ``args``) and ``send_tool_results`` rebuilds the
    ``functionCall`` parts from it. NOTE the divergence this creates from
    ``OpenAICompatSessionHandle.pending_tool_calls``' docstring, which describes that
    field as the RAW provider array: for this backend it holds the normalized array.

    **Every** ``functionCall`` part is returned, in block order — a Gemini turn can
    carry several parallel calls, and returning only the first is a named bug class in
    the plan (finding #5). Ids are synthesized ``call_0``/``call_1``/... from the
    BLOCK's position (see ``ToolCall``'s docstring in ``jsa/agents/base.py``): this API
    issues no call ids of its own, and none are needed on the way back either — a
    ``functionResponse`` is matched to its call by name and position, not by id. Keying
    on the block index rather than on a running count of collected calls mirrors
    ``anthropic_api.py``'s identical choice: two calls can then never collide on one id.

    ``arguments`` is passed through already decoded — Gemini's ``args`` is a JSON
    object, not the JSON *string* the OpenAI wire shape uses, and ``_parse_tool_calls``
    explicitly accepts an already-decoded object (some gateways do the same).
    """
    calls: list[dict] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        function_call = part.get("functionCall")
        if not isinstance(function_call, dict):
            continue
        calls.append(
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": function_call.get("name"),
                    "arguments": function_call.get("args"),
                },
            }
        )
    return calls


def _log_cached_content_tokens(backend_name: str, body: dict) -> None:
    """Log this response's implicitly-cached prompt-token count when one was reported.

    Extracted from the bottom of ``_call_api_once`` so a native tool-call reply gets
    the same observability a text reply does, without hoisting the read above the
    ``if not text`` raise (which would change when it fires for a non-tool reply).
    ``isinstance``-guarded: an API returning ``"usageMetadata": null`` or a wrong-typed
    field must not raise past a request that otherwise succeeded."""
    usage_metadata = body.get("usageMetadata")
    cached_tokens = (
        usage_metadata.get("cachedContentTokenCount")
        if isinstance(usage_metadata, dict)
        else None
    )
    if cached_tokens is not None:
        logger.info("%s API cachedContentTokenCount=%s", backend_name, cached_tokens)


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    """Build ``contents`` from the session's internal ``user``/``assistant`` message
    list, merging adjacent same-role turns into one content entry (multiple
    ``parts``) rather than emitting back-to-back same-role entries — see the module
    docstring's role-normalization note.

    Each entry contributes whatever ``_gemini_parts`` says it is: one ``{"text": ...}``
    part for an ordinary turn, or the pre-built ``functionCall``/``functionResponse``
    parts a tool-mode turn carries. The merge therefore ``extend``s rather than
    ``append``s. No MIXED merge actually arises in tool mode — the loop's turn sequence
    is user-text -> model-functionCall -> user-functionResponse -> model-functionCall,
    strictly alternating — so a functionResponse part never has to share a content
    entry with a text part; extending is simply the correct generalization, not a case
    being handled."""
    contents: list[dict] = []
    for m in messages:
        role = _to_gemini_role(m["role"])
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(_gemini_parts(m))
        else:
            contents.append({"role": role, "parts": _gemini_parts(m)})
    return contents


class GeminiBackend(OpenAICompatBackend):
    """AgentBackend implementation for Google's native Gemini ``generateContent`` API."""

    name = "gemini"
    env_vars = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    # Assesser's CHEAP tier (providers/tiers.py, verified Aug 2026) and confirmed
    # present + generateContent-capable in the live Phase-0 /models probe.
    default_model = "gemini-3.1-flash-lite"
    # :streamGenerateContent?alt=sse — content always; thinkingConfig thought
    # summaries when the configured model surfaces them. Never attempted in
    # structured mode (a partial JSON candidate is not useful to stream).
    supports_streaming = True

    def _reasoning_payload(self) -> dict[str, Any]:
        """``generationConfig.thinkingConfig`` — the only way to get Gemini to emit
        thought-summary parts. Without ``includeThoughts`` the API never sets
        ``"thought": true`` on any part, so ``_consume_gemini_sse``'s
        ``part.get("thought")`` split is never true and the reasoning channel stays
        empty even with streaming on.

        ``thinkingBudget`` is pinned to ``_THINKING_BUDGET`` rather than left unset
        (dynamic/unbounded thinking). Gemini counts thinking tokens against the SAME
        ``generationConfig.maxOutputTokens`` ceiling as the visible content — Google's
        own thinking guide confirms this and recommends capping the budget whenever a
        long response is expected. Left dynamic, a thinking-capable model (any Gemini
        model but the catalog's non-reasoning ``gemini-3.1-flash-lite`` default) can
        let its thought trace expand to consume most or all of ``_MAX_OUTPUT_TOKENS``
        before it ever writes the reply. Confirmed live against the ``cv_adjust``
        stage: the model completed the schema-required ``sections`` array (satisfying
        ``minItems: 1``, since ``Section.name``'s Pydantic default drops it from
        JSON-Schema ``required`` — see ``jsa/schema/cv.py``) with only a short
        "Summary" section and no Experience content at all — not a
        ``finishReason == 'MAX_TOKENS'`` truncation (that already raises
        ``ProtocolError`` above), but a schema-*valid*, budget-starved reply: with
        structured/controlled decoding the model must close out a well-formed JSON
        object within whatever room thinking left it, and dropping the token-heavy
        ``entries`` arrays is the cheapest way to do that. A fixed budget reserves a
        guaranteed floor of ``_MAX_OUTPUT_TOKENS`` for the actual CV/cover-letter JSON
        regardless of how much the model wants to think.

        Returned as a ``generationConfig`` fragment, not a top-level payload key —
        this backend overrides ``_call_api_once`` and merges it there; the shared
        base only ever calls this hook to decide whether reasoning fields were
        present. Not every Gemini model supports thinking (the catalog's own
        ``gemini-3.1-flash-lite`` default is one), and a model that doesn't answers
        4xx — hence the ``_ReasoningRejected`` degrade in ``_permanent_4xx`` below,
        which costs the thinking stream rather than the BF-19 slot."""
        if not self._reasoning:
            return {}
        return {"thinkingConfig": {"includeThoughts": True, "thinkingBudget": _THINKING_BUDGET}}

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        """Forwards ``tools`` to the inherited implementation, adding only the
        ``_SchemaRejected`` -> sentinel downgrade below.

        ``restore_session`` needs no override at all — the base's already accepts
        ``tools=`` and stores it on the handle, which is the entry point
        ``tool_loop.py`` actually uses (a revision always restores).

        Only ``_SchemaRejected`` is caught here. A ``_ToolsRejected`` from a tool-mode
        call is a ``ToolsUnsupported``, a hierarchy disjoint from
        ``AgentBackendUnavailable``, so it cannot be swallowed by this except clause
        and must never be given one of its own: the rung ladder in
        ``jsa/pipeline/tool_loop.py`` owns that recovery.
        """
        try:
            return await super().start_session(
                system_prompt, initial_user_msg, structured_schema, on_chunk, on_retry, tools
            )
        except _SchemaRejected as exc:
            if structured_schema is None:
                raise  # pragma: no cover — cannot occur, see _call_api_once
            logger.warning(
                "%s rejected the structured-output schema (%s) on session start — "
                "downgrading to sentinel mode and retrying once",
                self.name, exc,
            )
            return await super().start_session(
                system_prompt, initial_user_msg, None, on_chunk, on_retry, tools
            )

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> AgentReply:
        """As ``start_session``: only the schema rejection is caught and downgraded.
        The native tool vocabulary is resolved from the handle by the inherited
        implementation, and a ``_ToolsRejected`` raised on a tool turn propagates
        straight through this method — see ``start_session``'s docstring."""
        try:
            return await super().send_message(handle, text, structured_schema, on_chunk, on_retry)
        except _SchemaRejected as exc:
            logger.warning(
                "%s rejected the structured-output schema (%s) mid-session — "
                "downgrading to sentinel mode and retrying once",
                self.name, exc,
            )
            if isinstance(handle, OpenAICompatSessionHandle):
                handle.structured_enabled = False
            return await super().send_message(handle, text, None, on_chunk, on_retry)

    async def send_tool_results(
        self, handle: SessionHandle, results: list[ToolResult]
    ) -> AgentReply:
        """Continue a native tool session with the outcomes of one round of calls.

        Overridden (the only session method that has to be) because Gemini's wire
        shape for results has nothing in common with the base's ``{"role": "tool",
        "tool_call_id": ...}`` messages: one ``model`` turn of ``functionCall`` parts —
        replayed from ``handle.pending_tool_calls``, which this backend stores in the
        normalized OpenAI-compatible shape (see ``_function_calls_to_tool_calls``) and
        converts back here — followed by ONE ``user`` turn carrying a
        ``functionResponse`` part per result, in the same order. There is no ``"tool"``
        role on this API and no call id on a ``functionResponse``; the pairing is by
        name and position (see ``_to_gemini_role``'s docstring).

        ``tools`` stay attached to the follow-up request so the model can call again,
        and handle state is mutated ONLY after a successful call — the same discipline
        ``send_message`` uses, so a rejection or timeout leaves the conversation
        coherent for another rung to retry.
        """
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        if not handle.tools:
            # A caller bug, not a provider rejection. Without this the request would go
            # out with functionResponse parts and no declarations attached, 400, and be
            # classified by _permanent_4xx as... nothing tools-related at all (tools is
            # falsy), i.e. a _SchemaRejected/AgentBackendUnavailable that would cost the
            # job a BF-19 hop over a local mistake. Fail loudly instead.
            raise ValueError(
                "send_tool_results requires a native tool session — this handle has "
                "no tools (was it restored without tools=?)"
            )
        assistant_turn = {
            "role": "assistant",
            "parts": [
                {
                    "functionCall": {
                        "name": entry["function"]["name"],
                        "args": entry["function"].get("arguments") or {},
                    }
                }
                for entry in (handle.pending_tool_calls or [])
            ],
        }
        result_turn = {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": result.name,
                        # The applier's ok/error envelope is handed over verbatim as the
                        # response object — no separate error flag exists on this shape,
                        # and the model reads the envelope the applier produced.
                        "response": _as_response_object(result.content),
                    }
                }
                for result in results
            ],
        }
        pending_messages = handle.messages + [assistant_turn, result_turn]
        raw = await self._call_api(
            handle.system_prompt, pending_messages, None, None, None, handle.tools
        )
        if isinstance(raw, list):
            reply = _tool_calls_reply(_parse_tool_calls(raw, self.name))
            handle.messages.extend([assistant_turn, result_turn])
            handle.pending_tool_calls = raw
            return reply
        # Not another batch: the model answered text despite mode "ANY". Never nudged
        # for a sentinel (a tool session was never given that contract) — handed back
        # for tool_loop.py to abandon to rung 3, exactly as the shared base does.
        reply = _text_reply_in_tool_mode(raw)
        handle.messages.extend([assistant_turn, result_turn])
        handle.messages.append({"role": "assistant", "content": reply.raw})
        handle.pending_tool_calls = None
        return reply

    async def _consume_gemini_sse(self, response: httpx.Response, on_chunk: OnChunk) -> tuple[str, str]:
        """Drain a ``streamGenerateContent?alt=sse`` stream, forwarding text parts
        (and thought-summary parts, when a model actually emits them) through
        ``on_chunk`` as they arrive.

        Also tracks each event's ``finishReason`` (the last one seen wins, same
        as the non-streaming path reading it off the final response body) and
        raises the identical ``ProtocolError`` on ``MAX_TOKENS`` that the
        non-streaming branch raises below — a streamed reply that gets cut off
        must fail loudly the same way a buffered one does, not silently return
        the truncated partial text as if it were a complete reply.

        Returns ``(joined_content_text, raw_body_text)`` — ``raw_body_text`` is
        every line received, joined back together, so the caller can recover and
        classify a genuine HTTP-200 JSON error envelope when the "stream" wasn't
        SSE-shaped at all (mirrors _openai_compat.py's identical fallback)."""
        content_parts: list[str] = []
        raw_lines: list[str] = []
        finish_reason: str | None = None
        async for line in response.aiter_lines():
            if line:
                raw_lines.append(line)
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            candidates = event.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            if candidate.get("finishReason"):
                finish_reason = candidate["finishReason"]
            parts = (candidate.get("content") or {}).get("parts") or []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if not text:
                    # A per-part SKIP (unlike the `if not text` RAISE on the buffered
                    # path below), so a functionCall part would be dropped here in
                    # silence rather than misclassified. It is unreachable by
                    # construction all the same: tool mode never streams — `_call_api`
                    # forces stream_cb to None whenever tools are attached, and
                    # `_call_api_once` forces on_chunk to None again — so this loop is
                    # never entered on a tool-mode call. Do NOT "fix" it to reassemble
                    # fragmented functionCall parts; there are none to reassemble.
                    continue
                kind = "reasoning" if part.get("thought") else "content"
                if kind == "content":
                    content_parts.append(text)
                await on_chunk(AgentChunk(kind=kind, text=text))
        if finish_reason == "MAX_TOKENS":
            raise ProtocolError(
                "structured reply truncated: finishReason == 'MAX_TOKENS' "
                "(output cut off before the reply completed)"
            )
        return "".join(content_parts), "".join(raw_lines)

    async def _call_api_once(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ) -> str | list[dict]:
        """Single POST to ``{model}:generateContent``; classifies and raises on any
        failure. Never retries itself — the inherited ``_call_api`` wraps this in
        ``retry_transient``. Error classification mirrors
        ``OpenAICompatBackend._call_api_once``'s 3-way split (quota/rate ->
        ``AgentLimitReached`` unretried; transient overload/gateway ->
        ``TransientBackendError``, retried; permanent 4xx -> ``_SchemaRejected``/
        ``AgentBackendUnavailable`` unretried), adapted to Gemini's
        ``{"error": {"code", "message", "status"}}`` envelope shape.

        Returns the assistant's text, OR — when ``tools`` were attached and the model
        answered with function calls — the normalized ``tool_calls`` array (see
        ``_function_calls_to_tool_calls``), which is what the inherited
        ``start_session``/``send_message`` detect with ``isinstance(raw, list)``.

        ``tools`` is the LAST parameter because the inherited ``_call_api`` dispatches
        it as a conditional kwarg (``{"tools": tools} if tools else {}``) — an interim
        accommodation for this override not yet accepting one. Accepting it here is
        what makes ``supports_native_tools = True``, inherited from the base, true in
        fact for this backend rather than merely declared.
        """
        assert not (tools and structured_schema is not None), (
            "tool mode and structured mode are mutually exclusive per request — the "
            "terminal tool's arguments ARE the structured output, so no "
            "responseSchema may be sent alongside tools"
        )
        api_key = self._api_key()
        generation_config: dict[str, Any] = {"maxOutputTokens": _MAX_OUTPUT_TOKENS}
        generation_config.update(self._reasoning_payload())
        if structured_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = inline_defs(structured_schema)
        reasoning_fields_present = "thinkingConfig" in generation_config
        payload: dict[str, Any] = {
            "contents": _to_gemini_contents(messages),
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": generation_config,
        }
        tools_fields_present = bool(tools)
        if tools:
            # `to_gemini_function_declarations` (jsa/agents/tool_spec.py) already
            # strips `additionalProperties`, which this API's restricted OpenAPI-subset
            # schema rejects — the same family of restriction `inline_defs` works
            # around for `responseSchema`. Do not hand-roll the conversion here; and
            # note `inline_defs` is deliberately NOT applied to it: the tool schemas
            # are hand-written and $ref/$defs-free by construction (see that module's
            # docstring), so `inline_defs` keeps its "GeminiBackend is the only
            # caller" claim honestly, calling it only for `responseSchema` above.
            payload["tools"] = [{"functionDeclarations": to_gemini_function_declarations(tools)}]
            # mode "ANY" == the base's `tool_choice: "required"`: the model must call
            # SOME declared function, but picks which (mode "AUTO" would let it answer
            # prose, "NONE" would forbid calls entirely).
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}

        if tools:
            # Tool mode never streams — an invariant shared by every native-tools
            # backend here (the plan's finding #1). `_call_api` already forces its
            # stream_cb to None whenever tools are attached; forcing it again at this
            # level keeps the invariant true for a direct `_call_api_once` call too,
            # and is what makes `_consume_gemini_sse`'s lack of functionCall handling
            # correct rather than a gap.
            on_chunk = None

        # Streamed in BOTH modes. The `and structured_schema is None` that used to
        # be here made this dead code in practice: `_structured_schema_for` returns
        # a schema unconditionally for this backend, so every real pipeline call
        # fell to the buffered `generateContent` endpoint and nothing ever streamed.
        # The inherited `_call_api` already filters `on_chunk` down to reasoning
        # chunks only while structured (see `_openai_compat._reasoning_only`), and
        # `_consume_gemini_sse` excludes thought parts from the returned body — so
        # a partial-JSON content delta is never shown and never corrupts the parse.
        # The buffered branch below excludes them too; it did NOT until 2026-09-07,
        # which was invisible for exactly as long as this backend had no structured
        # caller that skips streaming (see the `not p.get("thought")` comment there).
        use_stream = on_chunk is not None
        if use_stream:
            url = f"{_API_BASE}/{self._model}:streamGenerateContent"
            params = {"alt": "sse"}
        else:
            url = f"{_API_BASE}/{self._model}:generateContent"
            params = None
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        streamed_content: str | None = None
        streamed_raw: str = ""
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            if use_stream:
                async with client.stream(
                    "POST", url, headers=headers, json=payload, params=params
                ) as response:
                    if response.status_code == 200:
                        streamed_content, streamed_raw = await self._consume_gemini_sse(response, on_chunk)
                    else:
                        await response.aread()
            else:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise AgentTimeout(f"{self.name} API timed out after {self._timeout}s") from None
        except httpx.HTTPError as exc:
            raise TransientBackendError(f"{self.name} API transport error: {exc}") from None
        finally:
            await client.aclose()

        def _permanent_4xx(detail: str) -> Exception:
            # Degrade precedence: tools -> reasoning -> caching -> fail, with this
            # backend's pre-existing schema downgrade layered underneath (there is no
            # cache_control field on this wire shape at all — Gemini's caching is
            # implicit — so the caching rung is a no-op here).
            #
            # Tools shed FIRST: `tools`/`toolConfig` is the newest and least-universally
            # -supported field, so it is the likeliest culprit, and unlike the two
            # enrichments it has a real functional fallback (tool_loop.py's prompt rung
            # still patches the document). Note the asymmetry and keep it: unlike
            # _ReasoningRejected below, _ToolsRejected is NOT caught and retried clean
            # anywhere in this backend — it is a ToolsUnsupported, and the inherited
            # `_call_api` degrade loop catches only _ReasoningRejected/_CacheRejected,
            # so it propagates to tool_loop.py, which owns the rung ladder. A catch
            # branch for it must never be added, here or in `_call_api`: retrying clean
            # would re-send a system prompt that explains a tool contract with no tools
            # attached, and hand the result back to the loop as if nothing had changed.
            #
            # Consequence worth knowing rather than fixing: with tools AND thinkingConfig
            # both on the wire, a reasoning-only rejection surfaces as _ToolsRejected and
            # costs the turn its native rung. The prompt rung then retries without tools,
            # where the reasoning degrade fires normally.
            if tools_fields_present:
                return _ToolsRejected(detail)
            # Reasoning degrade takes precedence over the schema downgrade: asking
            # for thought summaries is an optional enrichment, so a model that
            # rejects `thinkingConfig` must lose its thinking stream, not its
            # structured mode. The inherited `_call_api` catches this, clears
            # `self._reasoning`, and retries clean — a genuine schema rejection then
            # raises `_SchemaRejected` on that second attempt and downgrades as
            # before.
            if reasoning_fields_present:
                return _ReasoningRejected(detail)
            return _SchemaRejected(detail) if structured_schema is not None else AgentBackendUnavailable(detail)

        if response.status_code == 429:
            raise AgentLimitReached(f"{self.name} API rate limit reached: {response.text[:500]}")

        if streamed_content:
            return streamed_content

        if streamed_content == "":
            # HTTP 200, streamed, but no text content was extracted. Try to
            # recover the raw stream body as a JSON error envelope before
            # assuming a generic transient/empty-stream failure -- mirrors
            # _openai_compat.py's identical fallback for the same failure mode.
            try:
                body = json.loads(streamed_raw) if streamed_raw else None
            except ValueError:
                body = None
            if not (isinstance(body, dict) and "error" in body):
                raise TransientBackendError(
                    f"{self.name} API stream returned no text content"
                )
            # Fall through to the "error" in body classification below, using
            # the body recovered from the stream instead of response.json().
        else:
            try:
                body = response.json()
            except ValueError:
                detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
                if response.status_code >= 500:
                    raise TransientBackendError(detail) from None
                raise _permanent_4xx(detail) from None

        if isinstance(body, dict) and "error" in body:
            err = body["error"] if isinstance(body["error"], dict) else {}
            message = err.get("message", str(body["error"]))
            status = str(err.get("status", "")).upper()
            code = err.get("code", response.status_code)
            if code == 429 or status == "RESOURCE_EXHAUSTED" or "quota" in message.lower() or "rate" in message.lower():
                raise AgentLimitReached(f"{self.name} API limit reached: {message}")
            if 400 <= code < 500:
                raise _permanent_4xx(f"{self.name} API error: {message}")
            raise TransientBackendError(f"{self.name} API error: {message}")

        if response.status_code >= 400:
            detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail)
            raise _permanent_4xx(detail)

        candidates = body.get("candidates") or []
        if not candidates:
            raise TransientBackendError(
                f"{self.name} API returned no candidates: {response.text[:500]}"
            )
        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        # `not p.get("thought")` is load-bearing, and its absence was a live bug
        # (2026-09-07): with `includeThoughts` on, a buffered reply's `parts` carries the
        # thought summary AS A TEXT PART alongside the answer, so joining every part
        # prefixed the model's thinking onto the JSON and every structured parse died on
        # `invalid JSON (Expecting value: line 1 column 1 (char 0))`, downgrading the
        # session to sentinel mode on its first turn.
        #
        # `_consume_gemini_sse` has always split these correctly (`kind = "reasoning" if
        # part.get("thought")`), which is exactly why this went unnoticed: every
        # structured call in `stages.py` passes an `on_chunk` (Gemini declares
        # `supports_streaming`), so `use_stream` was always True and this branch was
        # unreachable in structured mode until `infer_structure.py` — which wires no
        # streaming — became the first structured caller to land here.
        #
        # A reply that is ALL thought and no answer now falls to the `if not text`
        # transient raise below instead of returning the thought prose as if it were
        # content. That is the correct classification: an empty answer is a failed turn.
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")
        )

        if finish_reason == "MAX_TOKENS":
            raise ProtocolError(
                "structured reply truncated: finishReason == 'MAX_TOKENS' "
                "(output cut off before the reply completed)"
            )

        if tools:
            # Extracted BEFORE the `if not text` raise below — the plan's finding #3.
            # A functionCall-only reply has NO text part, so that raise trips first and
            # classifies a working tool call as an empty (transient) reply: every retry
            # attempt burned, ending as AgentBackendUnavailable and dropping this
            # backend out of BF-19 over a good turn. Same load-bearing ordering as the
            # shared base's tool_calls-before-null-content check.
            #
            # Gated on `tools` deliberately: ungated, a stray functionCall part in a
            # NON-tool session would be returned as a list, and the inherited
            # start_session/send_message would hand run_stage a kind="tool_calls" reply
            # for a cv_adjust turn — the same hazard the plan names for a spontaneous
            # <<<TOOL_CALLS>>> block on the prompt rung.
            tool_calls = _function_calls_to_tool_calls(parts)
            if tool_calls:
                _log_cached_content_tokens(self.name, body)
                return tool_calls

        if not text:
            raise TransientBackendError(
                f"{self.name} API returned no text content: {response.text[:500]}"
            )
        _log_cached_content_tokens(self.name, body)
        return text
