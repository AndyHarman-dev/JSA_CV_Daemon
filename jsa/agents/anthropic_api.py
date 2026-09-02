"""AnthropicAPIBackend: REST API implementation of AgentBackend using the Anthropic SDK."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError, parse_reply
from jsa.schema.turn_models import parse_structured_reply_for_schema

logger = logging.getLogger(__name__)

# The single tool every structured-mode request offers. Its name is arbitrary — the
# model never gets to choose it as a "real" tool (tool_choice forces it) — it exists
# so the reply lands as a tool_use block whose input is schema-constrained JSON
# instead of free text. Forced tool-use (not the SDK's native output_config
# structured outputs) because output_config mandates additionalProperties: false
# recursively on every nested object, which the turn models' nested $defs
# (CVDocument/CoverLetter, extra="ignore" with defaulted optionals) do not satisfy;
# a tool's input_schema has no such recursive-strictness requirement.
_TOOL_NAME = "respond"


@dataclass(kw_only=True)
class AnthropicSessionHandle(SessionHandle):
    """Session handle for AnthropicAPIBackend; carries conversation history in memory."""
    id: str
    external_id: str | None  # always None — REST API is stateless; history is in Message rows
    system_prompt: str       # stored so each send_message call can pass it
    messages: list[dict] = field(default_factory=list)  # growing conversation list
    # The session's structured-output mode, established once at start/restore time.
    # Carried on the handle so follow-up send_message calls stay in the same mode
    # without re-passing the schema (capability ≠ per-call choice: a session is
    # either structured or sentinel for its whole life — no anthropic downgrade).
    structured_schema: dict[str, Any] | None = None


def _forced_tool_kwargs(schema: dict[str, Any]) -> dict[str, Any]:
    """Request kwargs that force the model to reply via the single ``respond`` tool.

    Request-building half of the thin forced-tool adapter; a future swap to the
    SDK's native response_format/output_config mechanism is contained inside this
    function and ``_extract_structured_text`` (the extraction half).
    """
    return {
        "tools": [{"name": _TOOL_NAME, "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def _extract_structured_text(response: Any) -> str:
    """Extraction half of the forced-tool adapter: canonical JSON text from a response.

    Checks ``stop_reason`` BEFORE extracting — a ``max_tokens`` stop means the tool
    input was cut off mid-stream, so whatever partial ``input`` the SDK managed to
    parse must never be treated as a complete reply (that truncation is a
    ProtocolError variant that Phase 5 routes into the self-heal budget: "output
    truncated; re-emit more concisely"). Then scans the content blocks for the
    ``tool_use`` block rather than indexing ``content[0]`` — a model under forced
    tool-use may still emit text blocks before the tool call.

    Returns the canonical union-JSON plain text (``json.dumps`` of the tool input),
    never the provider envelope — the canonical-form invariant (see
    jsa/schema/turn_models.py's module docstring) starts here. ``ensure_ascii=False``
    so the DB Message rows this text lands in (stages.py persists ``reply.raw``)
    stay human-readable UTF-8 for non-English CVs/letters, mirroring the sentinel
    path's naturally-unescaped raw model text; every consumer json.loads's it, so
    the escaping choice is semantically irrelevant (advisor-verified).
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ProtocolError(
            "structured reply truncated: stop_reason == 'max_tokens' "
            "(output cut off before the forced tool call completed)"
        )
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "tool_use":
            return json.dumps(block.input, ensure_ascii=False)
    raise ProtocolError(
        f"structured reply unparseable: no tool_use block in the response "
        f"(stop_reason={stop_reason!r})"
    )


def _parse_reply_for(raw: str, schema: dict[str, Any] | None) -> AgentReply:
    """Parse a reply per session mode: sentinel grammar, or the structured union-JSON."""
    if schema is None:
        return parse_reply(raw)
    return parse_structured_reply_for_schema(raw, schema)


class AnthropicAPIBackend(AgentBackend):
    """AgentBackend implementation that calls the Anthropic messages API directly.

    Structured mode (``structured_schema`` passed at session start) forces the
    model to reply through a single tool whose ``input_schema`` is the stage's
    turn-union schema; sentinel mode (no schema) is exactly the pre-structured
    behavior. ``supports_structured_output`` is True — the wire protocol can
    enforce a JSON schema on the reply (CLAUDE.md → backend fallback chain /
    structured-output plan "Locked decisions" #1).
    """

    name = "anthropic"
    supports_structured_output = True

    def __init__(
        self,
        model: str = "claude-haiku-4-5",
        timeout: float = 180.0,
        prompt_caching: bool = True,
    ) -> None:
        self._model = model
        self._timeout = timeout
        self._prompt_caching = prompt_caching

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> tuple[AnthropicSessionHandle, AgentReply]:
        """Open a fresh session: send the initial user message and return the handle + first reply."""
        messages: list[dict] = [{"role": "user", "content": initial_user_msg}]
        raw = await self._call_api(system_prompt, messages, structured_schema)
        reply = _parse_reply_for(raw, structured_schema)
        messages.append({"role": "assistant", "content": raw})
        handle = AnthropicSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
        )
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
    ) -> AnthropicSessionHandle:
        """Reconstruct a previously-ended session from persisted message history.

        Does NOT call the model — the restored handle is ready for send_message.
        The session's structured mode is (re)established here from the caller's
        schema argument, mirroring start_session.
        """
        messages = [{"role": turn.role, "content": turn.content} for turn in history]
        handle = AnthropicSessionHandle(
            id=str(uuid.uuid4()),
            external_id=None,
            system_prompt=system_prompt,
            messages=messages,
            structured_schema=structured_schema,
        )
        return handle

    async def send_message(
        self,
        handle: SessionHandle,
        text: str,
        structured_schema: dict[str, Any] | None = None,
    ) -> AgentReply:
        """Append a user turn, call the API, parse and store the assistant reply.

        Both turns are appended to handle.messages only after a successful API
        call, keeping the list coherent if the call times out or raises. The
        effective schema is the explicit kwarg if given, else the one the handle
        carries from start/restore time — omitting it keeps the session's mode.
        """
        if not isinstance(handle, AnthropicSessionHandle):
            raise TypeError(
                f"expected AnthropicSessionHandle, got {type(handle).__name__}"
            )
        schema = structured_schema if structured_schema is not None else handle.structured_schema
        pending_messages = handle.messages + [{"role": "user", "content": text}]
        raw = await self._call_api(handle.system_prompt, pending_messages, schema)
        reply = _parse_reply_for(raw, schema)
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

    async def _call_api(
        self,
        system_prompt: str,
        messages: list[dict],
        structured_schema: dict[str, Any] | None = None,
    ) -> str:
        """Call the Anthropic messages API and return the raw reply text.

        Sentinel mode (``structured_schema is None``) is otherwise the
        pre-structured behavior: no tools kwargs, first content block's text
        (``system`` becomes a cache-annotated block array when
        ``self._prompt_caching`` is on — see below — but that's a shape change,
        not a behavior change). Structured mode adds the forced-tool kwargs and
        extracts the canonical JSON from the tool_use block (see the two
        adapter functions above).

        The client is created per-call and explicitly closed in a finally block
        so the httpx connection pool is released on both normal exit and
        timeout cancellation.

        Unlike ClaudeCliBackend/GoogleCliBackend (see jsa/agents/_subprocess.py),
        this backend has no killable-subprocess problem to solve: `await
        client.messages.create(...)` is a real async operation, so
        Orchestrator.cancel_task()'s task.cancel() can interrupt it directly at
        this await point — no process to leak, nothing to kill.
        """
        import anthropic

        system: str | list[dict[str, Any]] = system_prompt
        if self._prompt_caching:
            # Default 5-minute TTL (no `ttl` key) — see CLAUDE.md "Prompt caching"
            # -> Phase 6: job-to-job reuse under continuous dispatch pays off well
            # inside a 5-minute start-to-start gap, so the 1-hour TTL's 2x write
            # premium buys nothing here. Tools render before system (CLAUDE.md's
            # documented render order), so this one breakpoint covers the forced-
            # tool schema too when structured_schema is set — no separate
            # cache_control on the tool definition is needed.
            system = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 32000,
            "system": system,
            "messages": messages,
        }
        if structured_schema is not None:
            create_kwargs.update(_forced_tool_kwargs(structured_schema))

        client = anthropic.AsyncAnthropic()
        try:
            response = await asyncio.wait_for(
                client.messages.create(**create_kwargs),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            raise AgentTimeout(
                f"Anthropic API timed out after {self._timeout}s"
            ) from None
        except anthropic.RateLimitError as exc:
            raise AgentLimitReached(
                f"Anthropic API rate limit reached: {exc}"
            ) from exc
        finally:
            await client.close()

        _log_cache_usage(response)

        if structured_schema is None:
            return response.content[0].text
        return _extract_structured_text(response)


def _log_cache_usage(response: Any) -> None:
    """Log cache_read/cache_creation token counts, isinstance-guarded.

    ``response.usage`` is normally a real SDK ``Usage`` object, but a test
    double or an SDK change could hand back something else entirely — guard
    with isinstance, not just attribute presence, so a wrong-typed usage
    object never raises past an otherwise-successful reply.
    """
    usage = getattr(response, "usage", None)
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)
    if isinstance(cache_read, int) or isinstance(cache_creation, int):
        logger.info(
            "anthropic API cache_read_input_tokens=%s cache_creation_input_tokens=%s",
            cache_read,
            cache_creation,
        )
