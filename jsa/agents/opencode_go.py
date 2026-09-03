"""OpenCodeGoBackend: dual-protocol HTTP backend for the OpenCode Go gateway.

Base URL: https://opencode.ai/zen/go/v1. Distinct from OpenCode Zen
(jsa/agents/opencode_zen.py) — different base URL, different API key
(OPENCODE_GO_API_KEY, falling back to OPENCODE_API_KEY), different model catalog.

This gateway spans two wire protocols per model, given by the module-level
``_PROTOCOL`` table (re-derived live against https://opencode.ai/docs/en/go/ during
Phase 2 implementation — see the multi-backend-model-select plan's Change Log for
per-entry provenance):

- ``"chat"`` — OpenAI-compatible ``/chat/completions``. Reuses
  ``jsa.agents._openai_compat.OpenAICompatBackend`` verbatim (via inheritance):
  structured output is enabled, with the same response_format/downgrade/retry
  machinery as Mistral and OpenRouter.
- ``"messages"`` — Anthropic-shape ``POST /v1/messages`` (``x-api-key`` auth, top-
  level ``system``, ``user``/``assistant``-only ``messages``). The reference
  bake-off project found forced tool-use does not take on this gateway path (the
  model answers free text regardless), and live probe #5 (Phase 0 Change Log)
  confirmed the ``x-api-key`` auth shape. This project answers that with the
  existing, well-tested sentinel path rather than prompt-injected JSON — so these
  models are **sentinel-only**, never structured.

An unknown model raises ``ValueError`` client-side, before any network call.
``supports_structured_output`` is set as an INSTANCE attribute in ``__init__`` from
the selected model's protocol — never read this off the class (see
``jsa/agents/base.py``'s ClassVar contract note); a class-level read would see the
inherited ``OpenAICompatBackend`` default of ``True`` and be wrong for a
``/messages`` model. ``jsa/pipeline/stages.py::_structured_schema_for`` already
reads ``backend.supports_structured_output`` off an instance, so this works
unmodified (verified via a repo-wide grep before landing this).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Literal

import httpx

from jsa.agents._openai_compat import (
    OpenAICompatBackend,
    OpenAICompatSessionHandle,
    TransientBackendError,
    _CacheRejected,
    _NUDGE_TEXT,
    retry_transient,
)
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentChunk,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    HistoryTurn,
    OnChunk,
    SessionHandle,
)
from jsa.agents.protocol import ProtocolError, parse_reply

logger = logging.getLogger(__name__)

_MESSAGES_ENDPOINT = "https://opencode.ai/zen/go/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"

Protocol = Literal["chat", "messages"]

# Re-derived live against https://opencode.ai/docs/en/go/ during Phase 2
# implementation (2026-08-31) — NOT captured by the Phase-0 probe script, which
# only confirmed the /messages auth header shape for one model (qwen3.8-max).
# Provenance: "glm-5.3" -> chat, "kimi-k3" -> chat, "qwen3.8-max" -> messages are
# corroborated by BOTH the live docs fetch AND the reference bake-off project's
# PROTOCOL_DISPATCH table (opencode_provider.py) plus live probe #5. Every other
# entry below is single-source (the live docs fetch only) — Phase 5's catalog
# curation should treat those ~20 as needing a live-listing cross-check, not as
# equally trustworthy as the three corroborated ones.
#
# /responses-protocol models (grok-4.6, gpt-5.6-luna, muse-spark-1.2-contributor)
# are deliberately excluded — this backend only dispatches "chat" and "messages"
# (see the plan's OpenCode-GO protocols decision). Selecting one of those raises
# ValueError, same as any other unrecognized model.
_PROTOCOL: dict[str, Protocol] = {
    # /chat/completions (OpenAI-compatible, structured output enabled)
    "glm-5.3-flash": "chat",
    "glm-5.3": "chat",
    "glm-5.2": "chat",
    "glm-5.1": "chat",
    "kimi-k3": "chat",
    "kimi-k2.7-code": "chat",
    "kimi-k2.6": "chat",
    "longcat-2.0": "chat",
    "deepseek-v4-pro": "chat",
    "deepseek-v4-flash": "chat",
    "deepseek-v4-flash-vision-exp": "chat",
    "mimo-v2.5": "chat",
    "mimo-v2.5-pro": "chat",
    "hy4-preview": "chat",
    "hy3": "chat",
    # /messages (Anthropic-shape, sentinel-only)
    "minimax-m3": "messages",
    "minimax-m2.7": "messages",
    "minimax-m2.5": "messages",
    "qwen3.8-max": "messages",
    "qwen3.8-flash": "messages",
    "qwen3.7-max": "messages",
    "qwen3.7-plus": "messages",
    "qwen3.6-plus": "messages",
}


class OpenCodeGoBackend(OpenAICompatBackend):
    """AgentBackend implementation for the OpenCode Go gateway (dual protocol)."""

    name = "opencode-go"
    # "chat" protocol streams for free via the inherited OpenAICompatBackend SSE
    # path; "messages" protocol gets its own hand-built stream below. Both are
    # speculative for this gateway (see the module docstring's forced-tool-use
    # gap precedent) — degrade to the synchronous reply on any streaming failure,
    # same best-effort contract as every other streaming backend.
    supports_streaming = True
    endpoint_url = "https://opencode.ai/zen/go/v1/chat/completions"
    env_vars = ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")
    # Corroborated "chat" model — see _PROTOCOL's provenance note above.
    default_model = "glm-5.3"

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 180.0,
        prompt_caching: bool = True,
    ) -> None:
        resolved_model = model if model is not None else self.default_model
        if resolved_model not in _PROTOCOL:
            raise ValueError(
                f"Unknown OpenCode Go model {resolved_model!r}. "
                f"Available: {sorted(_PROTOCOL)}"
            )
        super().__init__(model=resolved_model, timeout=timeout, prompt_caching=prompt_caching)
        self._protocol: Protocol = _PROTOCOL[resolved_model]
        # Instance-level override — see this module's docstring.
        self.supports_structured_output = self._protocol == "chat"

    def _system_content(self, system_prompt: str) -> str | list[dict]:
        """Speculative ``cache_control`` breakpoint for the ``/chat/completions``
        protocol — exactly OpenRouter's shape (jsa/agents/openrouter.py), so this
        inherits the Phase 4 ``_CacheRejected`` degrade-on-4xx from the shared base
        for free. Speculative because this gateway documents no cache API (see the
        module docstring's "reference bake-off project found forced tool-use does
        not take on this gateway path" precedent) — the degrade path is exactly
        what protects BF-19 if the guess is wrong. Never called for a ``/messages``
        model; that protocol's own cache_control shape lives in
        ``_call_messages_api_once`` below."""
        if not self._prompt_caching:
            return system_prompt
        return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        if self._protocol == "messages":
            return await self._start_session_messages(system_prompt, initial_user_msg, on_chunk)
        return await super().start_session(system_prompt, initial_user_msg, structured_schema, on_chunk)

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
    ) -> OpenAICompatSessionHandle:
        if self._protocol == "messages":
            return await self._restore_session_messages(system_prompt, history)
        return await super().restore_session(system_prompt, history, external_id, structured_schema)

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentReply:
        if self._protocol == "messages":
            return await self._send_message_messages(handle, text, on_chunk)
        return await super().send_message(handle, text, structured_schema, on_chunk)

    # end_session is inherited unchanged from OpenAICompatBackend — both protocols
    # share the same handle shape, and clearing handle.messages is protocol-agnostic.

    # ------------------------------------------------------------------
    # /messages protocol (Anthropic-shape, sentinel-only)
    # ------------------------------------------------------------------

    async def _start_session_messages(
        self, system_prompt: str, initial_user_msg: str, on_chunk: OnChunk | None = None
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_messages_api(system_prompt, messages, on_chunk)
        reply = await self._parse_with_nudge_messages(system_prompt, messages, raw)
        messages.append({"role": "assistant", "content": reply.raw})
        handle = OpenAICompatSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=None,
            structured_enabled=False,
        )
        return handle, reply

    async def _restore_session_messages(
        self, system_prompt: str, history: list[HistoryTurn]
    ) -> OpenAICompatSessionHandle:
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        return OpenAICompatSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=None,
            structured_enabled=False,
        )

    async def _send_message_messages(
        self, handle: SessionHandle, text: str, on_chunk: OnChunk | None = None
    ) -> AgentReply:
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_messages_api(handle.system_prompt, pending_messages, on_chunk)
        reply = await self._parse_with_nudge_messages(handle.system_prompt, pending_messages, raw)
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": reply.raw})
        return reply

    async def _parse_with_nudge_messages(
        self, system_prompt: str, messages: list[dict], raw: str
    ) -> AgentReply:
        """Same sentinel-nudge contract as OpenAICompatBackend._parse_with_nudge,
        but redoes the failed call via the /messages endpoint instead of
        /chat/completions."""
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
            nudge_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _NUDGE_TEXT},
            ]
            raw2 = await self._call_messages_api(system_prompt, nudge_messages)
            return parse_reply(raw2)  # Propagate on second failure

    async def _call_messages_api(
        self, system_prompt: str, messages: list[dict], on_chunk: OnChunk | None = None
    ) -> str:
        """POST to the /messages endpoint via retry_transient. A ``_CacheRejected``
        (a permanent 4xx while cache_control fields were present) is caught here,
        OUTSIDE ``retry_transient``'s budget — same rule and same mechanism as
        ``OpenAICompatBackend._call_api``'s degrade path (jsa/agents/
        _openai_compat.py), reused here because the ``/messages`` protocol does not
        go through that shared ``_call_api`` at all (see this module's docstring's
        dual-protocol split)."""
        try:
            return await retry_transient(
                lambda: self._call_messages_api_once(system_prompt, messages, on_chunk),
                unavailable_message=f"{self.name} API (messages) still failing",
            )
        except _CacheRejected as exc:
            logger.warning(
                "%s (messages) rejected the prompt-cache_control field (%s) — "
                "disabling prompt caching for this backend instance and retrying "
                "once without it",
                self.name, exc,
            )
            self._prompt_caching = False
            return await retry_transient(
                lambda: self._call_messages_api_once(system_prompt, messages, on_chunk),
                unavailable_message=f"{self.name} API (messages) still failing",
            )

    async def _consume_messages_sse(self, response: httpx.Response, on_chunk: OnChunk) -> str:
        """Drain an Anthropic Messages-shape SSE stream
        (``event: content_block_delta`` / ``data: {"delta": {"type": "text_delta",
        "text": ...}}``), forwarding content deltas through ``on_chunk``."""
        content_parts: list[str] = []
        async for line in response.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta") or {}
            if delta.get("type") != "text_delta":
                continue
            text = delta.get("text", "")
            if text:
                content_parts.append(text)
                await on_chunk(AgentChunk(kind="content", text=text))
        return "".join(content_parts)

    async def _call_messages_api_once(
        self, system_prompt: str, messages: list[dict], on_chunk: OnChunk | None = None
    ) -> str:
        """Single POST to the Anthropic-shape /messages endpoint; classifies and
        raises on any failure. Never retries itself — see _call_messages_api.

        Error classification mirrors OpenAICompatBackend._call_api_once's 3-way
        split (quota/rate -> AgentLimitReached unretried; transient overload/
        gateway -> TransientBackendError, retried by retry_transient; permanent
        4xx -> AgentBackendUnavailable unretried), adapted to the Anthropic
        Messages API's error envelope shape (``{"type": "error", "error": {"type":
        ..., "message": ...}}``) instead of the OpenAI-compatible one. Deliberately
        NOT built on the ``anthropic`` SDK (unlike jsa/agents/anthropic_api.py) —
        that backend has no AgentBackendUnavailable path at all (see the plan's
        Phase 2 "Problems" section), which is exactly the gap this module must not
        repeat.
        """
        api_key = self._api_key()
        system_content: str | list[dict]
        if self._prompt_caching:
            system_content = [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ]
        else:
            system_content = system_prompt
        cache_fields_present = isinstance(system_content, list)
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 32000,
            "system": system_content,
            "messages": messages,
        }
        use_stream = on_chunk is not None
        if use_stream:
            payload["stream"] = True
        headers = {
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        streamed_content: str | None = None
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            if use_stream:
                async with client.stream(
                    "POST", _MESSAGES_ENDPOINT, headers=headers, json=payload
                ) as response:
                    if response.status_code == 200:
                        streamed_content = await self._consume_messages_sse(response, on_chunk)
                    else:
                        await response.aread()
            else:
                response = await client.post(_MESSAGES_ENDPOINT, headers=headers, json=payload)
        except httpx.TimeoutException:
            raise AgentTimeout(
                f"{self.name} API (messages) timed out after {self._timeout}s"
            ) from None
        except httpx.HTTPError as exc:
            raise TransientBackendError(
                f"{self.name} API (messages) transport error: {exc}"
            ) from None
        finally:
            await client.aclose()

        if response.status_code == 429:
            raise AgentLimitReached(
                f"{self.name} API (messages) rate limit reached: {response.text[:500]}"
            )

        if streamed_content is not None:
            if not streamed_content:
                raise TransientBackendError(
                    f"{self.name} API (messages) stream returned no text content"
                )
            return streamed_content

        try:
            body = response.json()
        except ValueError:
            detail = f"{self.name} API (messages) error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail) from None
            raise AgentBackendUnavailable(detail) from None

        def _permanent_4xx(detail: str) -> Exception:
            return _CacheRejected(detail) if cache_fields_present else AgentBackendUnavailable(detail)

        if isinstance(body, dict) and body.get("type") == "error":
            err = body.get("error", {})
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if err_type == "rate_limit_error" or "rate" in message.lower():
                raise AgentLimitReached(f"{self.name} API (messages) limit reached: {message}")
            if 400 <= response.status_code < 500:
                raise _permanent_4xx(f"{self.name} API (messages) error: {message}")
            raise TransientBackendError(f"{self.name} API (messages) error: {message}")

        if response.status_code >= 400:
            detail = f"{self.name} API (messages) error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail)
            raise _permanent_4xx(detail)

        content_blocks = body.get("content") or []
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        content = "".join(text_parts)
        if not content:
            raise TransientBackendError(
                f"{self.name} API (messages) returned no text content: {response.text[:500]}"
            )
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            cache_read = usage.get("cache_read_input_tokens")
            cache_creation = usage.get("cache_creation_input_tokens")
            if cache_read is not None or cache_creation is not None:
                logger.info(
                    "%s (messages) cache_read_input_tokens=%s cache_creation_input_tokens=%s",
                    self.name, cache_read, cache_creation,
                )
        return content
