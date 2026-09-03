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
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from jsa.agents._openai_compat import OpenAICompatBackend, OpenAICompatSessionHandle, TransientBackendError
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentChunk,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    OnChunk,
    OnRetry,
    SessionHandle,
)
from jsa.agents.protocol import ProtocolError
from jsa.schema.turn_models import inline_defs

logger = logging.getLogger(__name__)

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_MAX_OUTPUT_TOKENS = 32000


class _SchemaRejected(AgentBackendUnavailable):
    """Internal only: a permanent 4xx from a structured-mode call, raised instead of
    plain ``AgentBackendUnavailable`` so ``start_session``/``send_message`` can catch
    it and downgrade to sentinel mode rather than dropping Gemini out of BF-19. See
    the module docstring's "Schema rejection must downgrade" note. Subclasses
    ``AgentBackendUnavailable`` as a safety net: an instance that somehow escapes
    both overrides still classifies correctly for BF-19."""


def _to_gemini_role(role: str) -> str:
    """Gemini's ``contents[].role`` is ``"user"`` or ``"model"`` — never
    ``"assistant"``, which is how every other backend's session handle stores it."""
    return "model" if role == "assistant" else "user"


def _to_gemini_contents(messages: list[dict]) -> list[dict]:
    """Build ``contents`` from the session's internal ``user``/``assistant`` message
    list, merging adjacent same-role turns into one content entry (multiple
    ``parts``) rather than emitting back-to-back same-role entries — see the module
    docstring's role-normalization note."""
    contents: list[dict] = []
    for m in messages:
        role = _to_gemini_role(m["role"])
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].append({"text": m["content"]})
        else:
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
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

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        try:
            return await super().start_session(
                system_prompt, initial_user_msg, structured_schema, on_chunk, on_retry
            )
        except _SchemaRejected as exc:
            if structured_schema is None:
                raise  # pragma: no cover — cannot occur, see _call_api_once
            logger.warning(
                "%s rejected the structured-output schema (%s) on session start — "
                "downgrading to sentinel mode and retrying once",
                self.name, exc,
            )
            return await super().start_session(system_prompt, initial_user_msg, None, on_chunk, on_retry)

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
        on_retry: OnRetry | None = None,
    ) -> AgentReply:
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
    ) -> str:
        """Single POST to ``{model}:generateContent``; classifies and raises on any
        failure. Never retries itself — the inherited ``_call_api`` wraps this in
        ``retry_transient``. Error classification mirrors
        ``OpenAICompatBackend._call_api_once``'s 3-way split (quota/rate ->
        ``AgentLimitReached`` unretried; transient overload/gateway ->
        ``TransientBackendError``, retried; permanent 4xx -> ``_SchemaRejected``/
        ``AgentBackendUnavailable`` unretried), adapted to Gemini's
        ``{"error": {"code", "message", "status"}}`` envelope shape.
        """
        api_key = self._api_key()
        generation_config: dict[str, Any] = {"maxOutputTokens": _MAX_OUTPUT_TOKENS}
        if structured_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = inline_defs(structured_schema)
        payload: dict[str, Any] = {
            "contents": _to_gemini_contents(messages),
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "generationConfig": generation_config,
        }

        use_stream = on_chunk is not None and structured_schema is None
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
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))

        if finish_reason == "MAX_TOKENS":
            raise ProtocolError(
                "structured reply truncated: finishReason == 'MAX_TOKENS' "
                "(output cut off before the reply completed)"
            )
        if not text:
            raise TransientBackendError(
                f"{self.name} API returned no text content: {response.text[:500]}"
            )
        usage_metadata = body.get("usageMetadata")
        cached_tokens = usage_metadata.get("cachedContentTokenCount") if isinstance(usage_metadata, dict) else None
        if cached_tokens is not None:
            logger.info("%s API cachedContentTokenCount=%s", self.name, cached_tokens)
        return text
