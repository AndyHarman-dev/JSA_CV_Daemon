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

import uuid
from typing import Any, Literal

import httpx

from jsa.agents._openai_compat import (
    OpenAICompatBackend,
    OpenAICompatSessionHandle,
    TransientBackendError,
    _NUDGE_TEXT,
    retry_transient,
)
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    HistoryTurn,
    SessionHandle,
)
from jsa.agents.protocol import ProtocolError, parse_reply

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

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        if self._protocol == "messages":
            return await self._start_session_messages(system_prompt, initial_user_msg)
        return await super().start_session(system_prompt, initial_user_msg, structured_schema)

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
    ) -> AgentReply:
        if self._protocol == "messages":
            return await self._send_message_messages(handle, text)
        return await super().send_message(handle, text, structured_schema)

    # end_session is inherited unchanged from OpenAICompatBackend — both protocols
    # share the same handle shape, and clearing handle.messages is protocol-agnostic.

    # ------------------------------------------------------------------
    # /messages protocol (Anthropic-shape, sentinel-only)
    # ------------------------------------------------------------------

    async def _start_session_messages(
        self, system_prompt: str, initial_user_msg: str
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_messages_api(system_prompt, messages)
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

    async def _send_message_messages(self, handle: SessionHandle, text: str) -> AgentReply:
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_messages_api(handle.system_prompt, pending_messages)
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

    async def _call_messages_api(self, system_prompt: str, messages: list[dict]) -> str:
        return await retry_transient(
            lambda: self._call_messages_api_once(system_prompt, messages),
            unavailable_message=f"{self.name} API (messages) still failing",
        )

    async def _call_messages_api_once(self, system_prompt: str, messages: list[dict]) -> str:
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
        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 8192,
            "system": system_prompt,
            "messages": messages,
        }
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                _MESSAGES_ENDPOINT,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
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

        try:
            body = response.json()
        except ValueError:
            detail = f"{self.name} API (messages) error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail) from None
            raise AgentBackendUnavailable(detail) from None

        if isinstance(body, dict) and body.get("type") == "error":
            err = body.get("error", {})
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if err_type == "rate_limit_error" or "rate" in message.lower():
                raise AgentLimitReached(f"{self.name} API (messages) limit reached: {message}")
            if 400 <= response.status_code < 500:
                raise AgentBackendUnavailable(f"{self.name} API (messages) error: {message}")
            raise TransientBackendError(f"{self.name} API (messages) error: {message}")

        if response.status_code >= 400:
            detail = f"{self.name} API (messages) error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail)
            raise AgentBackendUnavailable(detail)

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
        return content
