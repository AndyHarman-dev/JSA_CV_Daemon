"""AnthropicAPIBackend: REST API implementation of AgentBackend using the Anthropic SDK."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from jsa.agents.base import AgentBackend, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import parse_reply


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

    def __init__(self, model: str = "claude-haiku-4-5", timeout: float = 180.0) -> None:
        self._model = model
        self._timeout = timeout

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
        """No-op for stateless REST API — just clear in-memory history."""
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        handle.messages.clear()

    async def _call_api(self, system_prompt: str, messages: list[dict]) -> str:
        """Call the Anthropic messages API and return the raw text response.

        The client is created per-call and explicitly closed in a finally block
        so the httpx connection pool is released on both normal exit and
        timeout cancellation.
        """
        import anthropic

        client = anthropic.AsyncAnthropic()
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model=self._model,
                    max_tokens=8192,
                    system=system_prompt,
                    messages=messages,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise AgentTimeout(
                f"Anthropic API timed out after {self._timeout}s"
            ) from None
        finally:
            await client.close()
        return response.content[0].text
