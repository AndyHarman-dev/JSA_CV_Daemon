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

import os
import uuid
from dataclasses import dataclass, field

import httpx

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import parse_reply

_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"


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
        reply = parse_reply(raw)
        messages.append({"role": "assistant", "content": raw})
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
        reply = parse_reply(raw)
        # Mutate only after success so handle stays consistent on error
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": raw})
        return reply

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op for stateless REST API — just clear in-memory history."""
        if not isinstance(handle, OpenCodeZenSessionHandle):
            raise TypeError(
                f"expected OpenCodeZenSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(self, system_prompt: str, messages: list[dict]) -> str:
        """POST to the OpenCode Zen chat-completions endpoint and return the raw reply text.

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
            raise AgentTimeout(
                f"OpenCode Zen API timed out after {self._timeout}s"
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
            raise RuntimeError(
                f"OpenCode Zen API error {response.status_code}: {response.text[:500]}"
            ) from None

        if "error" in body:
            err = body["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            err_type = err.get("type", "") if isinstance(err, dict) else ""
            if "rate" in message.lower() or "credit" in message.lower() or err_type in {"RateLimitError", "CreditsError"}:
                raise AgentLimitReached(f"OpenCode Zen API limit reached: {message}")
            raise RuntimeError(f"OpenCode Zen API error: {message}")

        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenCode Zen API error {response.status_code}: {response.text[:500]}"
            )

        content = body["choices"][0]["message"]["content"]
        if content is None:
            # Reasoning models can return a null content when the reply is all
            # `reasoning` (e.g. truncated by max_tokens before any final answer) —
            # surface this as a clear error rather than letting parse_reply's regex
            # crash on None.
            raise RuntimeError(
                "OpenCode Zen API returned null message content (model may have "
                "produced only reasoning tokens before hitting max_tokens)"
            )
        return content
