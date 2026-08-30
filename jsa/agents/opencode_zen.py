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
import logging
import os
import uuid
from dataclasses import dataclass, field

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

logger = logging.getLogger(__name__)

_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"

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


@dataclass(kw_only=True)
class OpenCodeZenSessionHandle(SessionHandle):
    """Session handle for OpenCodeZenBackend; carries conversation history in memory."""
    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can rebuild the messages array
    messages: list[dict] = field(default_factory=list)  # growing conversation list (no system entry)


class OpenCodeZenBackend(AgentBackend):
    """AgentBackend implementation that calls the OpenCode Zen chat-completions API."""

    name = "opencode-zen"

    def __init__(self, model: str = "nemotron-3-ultra-free", timeout: float = 180.0) -> None:
        self._model = model
        self._timeout = timeout

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[OpenCodeZenSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(system_prompt, messages)
        reply = await self._parse_with_nudge(system_prompt, messages, raw)
        messages.append({"role": "assistant", "content": reply.raw})
        handle = OpenCodeZenSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
        )
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> OpenCodeZenSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = OpenCodeZenSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
        )
        return handle

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Append a user turn, call the API, parse and store the assistant reply.

        Both turns are appended to handle.messages only after a successful API
        call, keeping the list coherent if the call times out or raises.
        """
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
            )
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages)
        reply = await self._parse_with_nudge(handle.system_prompt, pending_messages, raw)
        # Mutate only after success so handle stays consistent on error
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": reply.raw})
        return reply

    async def _parse_with_nudge(self, system_prompt: str, messages: list[dict], raw: str) -> AgentReply:
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
        """
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
            nudge_messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": _NUDGE_TEXT},
            ]
            raw2 = await self._call_api(system_prompt, nudge_messages)
            return parse_reply(raw2)  # Propagate on second failure

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op for stateless REST API — just clear in-memory history."""
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(self, system_prompt: str, messages: list[dict]) -> str:
        """POST to the OpenCode Zen endpoint, retrying transient overload/gateway
        failures on THIS backend up to _MAX_ATTEMPTS before giving up.

        A rate limit (AgentLimitReached), a timeout (AgentTimeout), or a permanent
        config/client error (AgentBackendUnavailable — bad model, bad key, malformed
        request) all propagate immediately, unretried: none of those are fixed by
        calling the same backend again, so retrying them would only burn the fit/
        cv_adjust/cover_letter stage's wall-clock budget before BF-19 ever gets a
        chance to switch backends. Only the free-tier's known-flaky overload/gateway
        signals (raised as _TransientOpenCodeError by _call_api_once) get retried here.
        """
        last_exc: _TransientOpenCodeError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await self._call_api_once(system_prompt, messages)
            except _TransientOpenCodeError as exc:
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    logger.warning(
                        "OpenCode Zen API transient failure (attempt %d/%d), retrying: %s",
                        attempt + 1, _MAX_ATTEMPTS, exc,
                    )
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt])
                    continue
        raise AgentBackendUnavailable(
            f"OpenCode Zen API still failing after {_MAX_ATTEMPTS} attempts: {last_exc}"
        ) from None

    async def _call_api_once(self, system_prompt: str, messages: list[dict]) -> str:
        """Single POST to the OpenCode Zen chat-completions endpoint; classifies
        and raises on any failure. Never retries itself — see _call_api.

        The client is created per-call and explicitly closed in a finally block,
        matching AnthropicAPIBackend's connection-pool hygiene. `await
        client.post(...)` is a real async operation, so Orchestrator.cancel_task()'s
        task.cancel() can interrupt it directly at this await point.
        """
        api_key = os.environ.get("OPENCODE_API_KEY", "")
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "max_tokens": 8192,
        }
        client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
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

        # A gateway-level failure (e.g. a 502/503 from the proxy in front of the API,
        # as opposed to the API's own JSON error envelope) can return non-JSON — HTML,
        # plain text. Check that *before* touching status_code>=400 below, since that
        # branch depends on `body` and must never be reached via a JSONDecodeError.
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
