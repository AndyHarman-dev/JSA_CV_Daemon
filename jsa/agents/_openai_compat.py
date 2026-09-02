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
per-session structured→sentinel downgrade, and the sentinel-compliance nudge. Concrete
subclasses (``MistralBackend``, ``OpenRouterBackend``, and the ``/chat/completions`` half
of ``OpenCodeGoBackend``) need only set ``name``, ``endpoint_url``, ``env_vars``, and
``default_model`` as class attributes; ``_extra_payload`` is an optional hook for a
subclass-specific top-level payload key (OpenRouter's routing guard).
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, ClassVar

import httpx

from jsa.agents.base import (
    AgentBackend,
    AgentBackendUnavailable,
    AgentLimitReached,
    AgentReply,
    AgentTimeout,
    HistoryTurn,
    SessionHandle,
)
from jsa.agents.protocol import ProtocolError, parse_reply
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


def _active_schema(
    handle: OpenAICompatSessionHandle, explicit: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The schema actually in force for one call: None once the session has
    downgraded, regardless of what the caller (stages.py) keeps passing in."""
    if not handle.structured_enabled:
        return None
    return explicit if explicit is not None else handle.structured_schema


async def retry_transient(
    call_once: Callable[[], Awaitable[str]],
    *,
    unavailable_message: str,
    max_attempts: int = _MAX_ATTEMPTS,
    backoff: tuple[float, ...] = _RETRY_BACKOFF_SECONDS,
) -> str:
    """Call ``call_once()`` up to ``max_attempts`` times, retrying only on
    ``TransientBackendError`` (with a short sleep between attempts) and converting
    an exhausted budget into ``AgentBackendUnavailable``. Any other exception
    (``AgentLimitReached``, ``AgentTimeout``, a permanent-4xx
    ``AgentBackendUnavailable``) propagates immediately, unretried — see
    ``OpenAICompatBackend._call_api_once``'s docstring for why.
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

    def _extra_payload(self, system_prompt: str) -> dict[str, Any]:
        """Hook for subclass-specific top-level payload keys. Default: none.
        OpenRouter overrides this to add its mandatory routing guard; Mistral
        overrides it to add a ``prompt_cache_key`` derived from ``system_prompt``."""
        return {}

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
    ) -> tuple[OpenAICompatSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(system_prompt, messages, structured_schema)
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            system_prompt, messages, raw, structured_schema
        )
        messages.append({"role": "assistant", "content": reply.raw})
        handle = OpenAICompatSessionHandle(
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
    ) -> OpenAICompatSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        return OpenAICompatSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
            structured_enabled=structured_schema is not None,
        )

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> AgentReply:
        """Append a user turn, call the API, parse and store the assistant reply.

        Both turns are appended to handle.messages only after a successful API
        call, keeping the list coherent if the call times out or raises.
        """
        if not isinstance(handle, OpenAICompatSessionHandle):
            raise TypeError(
                f"expected OpenAICompatSessionHandle, got {type(handle).__name__}"
            )
        schema = _active_schema(handle, structured_schema)
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages, schema)
        reply, structured_enabled = await self._parse_structured_with_downgrade(
            handle.system_prompt, pending_messages, raw, schema
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
    ) -> tuple[AgentReply, bool]:
        """Parse ``raw`` per the session's current mode; downgrade to sentinel mode
        on an unparseable/missing-``kind`` structured reply. See
        ``opencode_zen.py``'s method of the same name for the full rationale — this
        is behaviourally identical, just backend-name-agnostic in its log message.
        """
        if schema is None:
            return await self._parse_with_nudge(system_prompt, messages, raw), False
        try:
            return parse_structured_reply_for_schema(raw, schema), True
        except ProtocolError as exc:
            logger.warning(
                "%s structured reply unparseable (%s) — downgrading this session "
                "to sentinel mode for its remaining turns",
                self.name, exc,
            )
            reply = await self._parse_with_nudge(
                system_prompt, messages, raw, nudge_text=_DOWNGRADE_NUDGE_TEXT
            )
            return reply, False

    async def _parse_with_nudge(
        self,
        system_prompt: str,
        messages: list[dict],
        raw: str,
        nudge_text: str = _NUDGE_TEXT,
    ) -> AgentReply:
        """Try parse_reply(raw); on 'no sentinel block' ProtocolError, nudge once.
        Any other ProtocolError, or a second failure, is re-raised immediately.
        See ``opencode_zen.py``'s method of the same name for the full rationale.
        """
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
            nudge_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": nudge_text},
            ]
            raw2 = await self._call_api(system_prompt, nudge_messages)
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
    ) -> str:
        """POST to ``endpoint_url``, retrying transient overload/gateway failures on
        THIS backend up to ``_MAX_ATTEMPTS`` before giving up. See
        ``opencode_zen.py``'s method of the same name for the full rationale."""
        return await retry_transient(
            lambda: self._call_api_once(system_prompt, messages, structured_schema),
            unavailable_message=f"{self.name} API still failing after {_MAX_ATTEMPTS} attempts",
        )

    async def _call_api_once(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
    ) -> str:
        """Single POST to the chat-completions endpoint; classifies and raises on
        any failure. Never retries itself — see _call_api. Mirrors
        ``opencode_zen.py``'s method of the same name; see its docstring for the
        full rationale behind each classification branch.
        """
        api_key = self._api_key()
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_tokens": 8192,
        }
        payload.update(self._extra_payload(system_prompt))
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
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                self.endpoint_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
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

        if response.status_code == 429:
            raise AgentLimitReached(f"{self.name} API rate limit reached: {response.text[:500]}")

        try:
            body = response.json()
        except ValueError:
            detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail) from None
            raise AgentBackendUnavailable(detail) from None

        if "error" in body:
            err = body["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if "rate" in message.lower() or "credit" in message.lower() or err_type in {"RateLimitError", "CreditsError"}:
                raise AgentLimitReached(f"{self.name} API limit reached: {message}")
            if 400 <= response.status_code < 500:
                raise AgentBackendUnavailable(f"{self.name} API error: {message}")
            raise TransientBackendError(f"{self.name} API error: {message}")

        if response.status_code >= 400:
            detail = f"{self.name} API error {response.status_code}: {response.text[:500]}"
            if response.status_code >= 500:
                raise TransientBackendError(detail)
            raise AgentBackendUnavailable(detail)

        choices = body.get("choices")
        if not choices:
            raise TransientBackendError(
                f"{self.name} API returned no choices: {response.text[:500]}"
            )
        content = choices[0].get("message", {}).get("content")
        if content is None:
            raise TransientBackendError(
                f"{self.name} API returned null message content (model may have "
                "produced only reasoning tokens before hitting max_tokens)"
            )
        usage = body.get("usage")
        prompt_tokens_details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
        cached_tokens = prompt_tokens_details.get("cached_tokens") if isinstance(prompt_tokens_details, dict) else None
        if cached_tokens is not None:
            logger.info("%s API cached_tokens=%s", self.name, cached_tokens)
        return content
