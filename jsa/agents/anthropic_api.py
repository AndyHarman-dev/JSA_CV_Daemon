"""AnthropicAPIBackend: REST API implementation of AgentBackend using the Anthropic SDK."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from jsa.agents.base import (
    AgentBackend,
    AgentLimitReached,
    AgentOutputTruncated,
    AgentReply,
    AgentRequestError,
    AgentTimeout,
    HistoryTurn,
    SessionHandle,
)
from jsa.agents.protocol import parse_reply


def require_api_key() -> None:
    """Raise a clear, actionable error if ``ANTHROPIC_API_KEY`` is not set.

    Called from ``jsa.server.make_backend_factory`` before constructing the
    anthropic backend, so a missing key is reported once, up front, with an
    actionable message — instead of surfacing as an opaque error from deep
    inside the Anthropic SDK's client constructor in the middle of a job run.

    Deliberately NOT called from ``AnthropicAPIBackend.__init__``: unit tests
    construct the backend directly (mocking ``anthropic.AsyncAnthropic``) and
    must keep working without the env var set.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable is not set — required "
            "for the anthropic backend. Set it before starting jsa, or "
            "choose a different backend (e.g. --backend claude-cli)."
        )


@dataclass(kw_only=True)
class AnthropicSessionHandle(SessionHandle):
    """Session handle for AnthropicAPIBackend; carries conversation history in memory."""
    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can pass it
    messages: list[dict] = field(default_factory=list)  # growing conversation list


class AnthropicAPIBackend(AgentBackend):
    """AgentBackend implementation that calls the Anthropic messages API directly."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        timeout: float = 180.0,
        max_tokens: int = 16384,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens
        # Lazily constructed, then reused for the lifetime of this backend
        # instance — one httpx connection pool shared across every turn of
        # every session, instead of a fresh pool per API call.
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Return the shared AsyncAnthropic client, constructing it on first use."""
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[AnthropicSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(system_prompt, messages)
        reply = parse_reply(raw)
        messages.append({"role": "assistant", "content": raw})
        handle = AnthropicSessionHandle(
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
    ) -> AnthropicSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = AnthropicSessionHandle(
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
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages)
        reply = parse_reply(raw)
        # Mutate only after success so handle stays consistent on error
        handle.messages.append({"role": "user", "content": text})
        handle.messages.append({"role": "assistant", "content": raw})
        return reply

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op for stateless REST API — just clear in-memory history.

        The shared httpx client is intentionally NOT closed here: it is
        reused across sessions and jobs for the lifetime of this backend
        instance (a typical long-lived pattern for an async httpx client).
        """
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(self, system_prompt: str, messages: list[dict]) -> str:
        """Call the Anthropic messages API and return the raw text response.

        Uses the shared client from ``_get_client()`` (see class docstring on
        connection reuse). Unlike ClaudeCliBackend/GoogleCliBackend (see
        jsa/agents/_subprocess.py), this backend has no killable-subprocess
        problem to solve: `await client.messages.create(...)` is a real async
        operation, so Orchestrator.cancel_task()'s task.cancel() can interrupt
        it directly at this await point — no process to leak, nothing to kill.

        The system prompt is sent as a content-block list with a
        ``cache_control`` breakpoint so its (large, turn-invariant) text is
        served from Anthropic's prompt cache on every turn after the first,
        instead of being re-billed at full input price each time.
        """
        import anthropic

        client = self._get_client()
        system_param = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    system=system_param,
                    messages=messages,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise AgentTimeout(
                f"Anthropic API timed out after {self._timeout}s"
            ) from None
        except anthropic.BadRequestError as exc:
            # Non-retryable: the request itself is malformed (e.g. an invalid
            # max_tokens value, or a schema violation). Retrying the same
            # request unchanged will fail the same way.
            raise AgentRequestError(
                f"Anthropic API rejected the request as invalid: {exc}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AgentLimitReached(
                f"Anthropic API rate limit reached: {exc}"
            ) from exc
        except anthropic.APITimeoutError as exc:
            # Subclass of APIConnectionError — must be caught before it.
            raise AgentTimeout(
                f"Anthropic API request timed out: {exc}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AgentTimeout(
                f"Anthropic API connection error: {exc}"
            ) from exc
        except anthropic.APIStatusError as exc:
            # Catches BadRequestError/RateLimitError subclasses too, but both
            # are handled above; this is the remaining 4xx/5xx fallback
            # (AuthenticationError, PermissionDeniedError, NotFoundError,
            # InternalServerError, OverloadedError, ServiceUnavailableError...).
            if exc.status_code >= 500:
                # Server-side error — transient, worth retrying.
                raise AgentTimeout(
                    f"Anthropic API server error ({exc.status_code}): {exc}"
                ) from exc
            raise AgentRequestError(
                f"Anthropic API error ({exc.status_code}): {exc}"
            ) from exc

        if response.stop_reason == "max_tokens":
            raise AgentOutputTruncated(
                "Anthropic response was truncated at max_tokens="
                f"{self._max_tokens} before the model finished — increase "
                "max_tokens (Settings.max_tokens / JSA_MAX_TOKENS) or "
                "shorten the input."
            )
        if not response.content:
            raise AgentRequestError(
                "Anthropic API returned an empty content list — nothing to parse."
            )
        block = response.content[0]
        if not hasattr(block, "text"):
            block_type = getattr(block, "type", type(block).__name__)
            raise AgentRequestError(
                f"Anthropic API's first content block has no text (type={block_type!r})."
            )
        return block.text
