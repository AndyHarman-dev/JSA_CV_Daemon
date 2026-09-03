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
)
from jsa.agents.protocol import ProtocolError, parse_reply
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
    ) -> tuple[OpenCodeZenSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(system_prompt, messages, structured_schema, on_chunk, on_retry)
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
    ) -> OpenCodeZenSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        The session's structured mode is (re)established here from the caller's
        schema argument, mirroring AnthropicSessionHandle — never defaulted to
        True unconditionally (see OpenCodeZenSessionHandle.structured_enabled).
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = OpenCodeZenSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
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
        schema = _active_schema(handle, structured_schema)
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages, schema, on_chunk, on_retry)
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            handle.system_prompt, pending_messages, raw, schema, on_chunk, on_retry
        )
        # Mutate only after success so handle stays consistent on error
        handle.structured_enabled = structured_enabled
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": reply.raw})
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
    ) -> str:
        """POST to the OpenCode Zen endpoint, retrying transient overload/gateway
        failures on THIS backend up to _MAX_ATTEMPTS before giving up.

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
        """
        stream_cb = on_chunk if structured_schema is None else None
        retry_cb = on_retry if structured_schema is None else None
        last_exc: _TransientOpenCodeError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await self._call_api_once(system_prompt, messages, structured_schema, stream_cb)
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
    ) -> str:
        """Single POST to the OpenCode Zen chat-completions endpoint; classifies
        and raises on any failure. Never retries itself — see _call_api.

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
                raise AgentBackendUnavailable(detail) from None

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
                raise AgentBackendUnavailable(f"OpenCode Zen API error: {message}")
            raise _TransientOpenCodeError(f"OpenCode Zen API error: {message}")

        if response.status_code >= 400:
            detail = f"OpenCode Zen API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise _TransientOpenCodeError(detail)
            raise AgentBackendUnavailable(detail)

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
        content = choices[0].get("message", {}).get("content")
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
