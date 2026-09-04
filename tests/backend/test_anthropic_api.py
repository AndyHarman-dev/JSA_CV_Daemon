"""Tests for jsa/agents/anthropic_api.py: AnthropicAPIBackend and AnthropicSessionHandle.

The `import anthropic` occurs *inside* `_call_api`, not at module scope, so we patch
`anthropic.AsyncAnthropic` directly on the SDK module rather than at the file attribute.

All tests are async; asyncio_mode = "auto" is set in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents.anthropic_api import (
    AnthropicAPIBackend,
    AnthropicSessionHandle,
    _ToolsRejected,
)
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentLimitReached,
    AgentTimeout,
    HistoryTurn,
    SessionHandle,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import to_anthropic_tools, tools_for
from jsa.db.models import Stage
from jsa.schema.cv import CVDocument
from jsa.schema.turn_models import json_schema_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"
NEED_INPUT_RAW = "<<<NEED_INPUT>>>\nWhat is your target industry?\n<<<END>>>"


def _make_mock_client(text: str) -> MagicMock:
    """Return an async-compatible mock client whose messages.create returns `text`.

    `close` is also an AsyncMock so `await client.close()` works in _call_api's
    finally block.
    """
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    return mock_client


def _text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _tool_block(tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    return block


def _make_mock_block_client(
    content_blocks: list,
    stop_reason: str = "tool_use",
) -> MagicMock:
    """Mock client whose messages.create returns an arbitrary block list + stop_reason.

    Used for structured-mode responses (tool_use blocks, truncation, missing
    tool_use) — the caller composes the exact content-block scenario.
    """
    mock_response = MagicMock()
    mock_response.stop_reason = stop_reason
    mock_response.content = content_blocks
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    return mock_client


# ---------------------------------------------------------------------------
# 1 — start_session: happy path with FINAL reply
# ---------------------------------------------------------------------------

class TestStartSessionFinal:
    async def test_kind_is_final(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session("sys", "initial user message")
        assert reply.kind == "final"

    async def test_content_is_inner_text(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session("sys", "initial user message")
        assert reply.content == "Adjusted CV content here."

    async def test_handle_external_id_is_none(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert handle.external_id is None

    async def test_handle_messages_contains_user_and_assistant_turns(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert len(handle.messages) == 2
        assert handle.messages[0] == {"role": "user", "content": "initial user message"}
        # The assistant turn stores raw text (full sentinel), not just content
        assert handle.messages[1] == {"role": "assistant", "content": FINAL_RAW}

    async def test_handle_is_anthropic_session_handle(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert isinstance(handle, AnthropicSessionHandle)

    async def test_handle_system_prompt_stored(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("my system prompt", "msg")
        assert handle.system_prompt == "my system prompt"


# ---------------------------------------------------------------------------
# 2 — start_session: NEED_INPUT reply
# ---------------------------------------------------------------------------

class TestStartSessionNeedInput:
    async def test_kind_is_needs_input(self):
        mock_client = _make_mock_client(NEED_INPUT_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session("sys", "hello")
        assert reply.kind == "needs_input"

    async def test_question_is_populated(self):
        mock_client = _make_mock_client(NEED_INPUT_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session("sys", "hello")
        assert reply.question == "What is your target industry?"

    async def test_content_matches_question(self):
        mock_client = _make_mock_client(NEED_INPUT_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session("sys", "hello")
        assert reply.content == reply.question


# ---------------------------------------------------------------------------
# 3 — restore_session: no API call, history reconstructed
# ---------------------------------------------------------------------------

class TestRestoreSession:
    async def test_no_api_call_is_made(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            history = [
                HistoryTurn(role="user", content="q"),
                HistoryTurn(role="assistant", content="a"),
            ]
            await backend.restore_session("sys", history, external_id=None)
        # messages.create must NOT have been called
        mock_client.messages.create.assert_not_called()

    async def test_messages_reconstructed_from_history(self):
        backend = AnthropicAPIBackend()
        history = [
            HistoryTurn(role="user", content="q"),
            HistoryTurn(role="assistant", content="a"),
        ]
        handle = await backend.restore_session("sys", history, external_id=None)
        assert handle.messages == [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]

    async def test_external_id_is_none(self):
        backend = AnthropicAPIBackend()
        history = [HistoryTurn(role="user", content="q"), HistoryTurn(role="assistant", content="a")]
        handle = await backend.restore_session("sys", history, external_id=None)
        assert handle.external_id is None

    async def test_external_id_ignored_always_none(self):
        """REST API is stateless; external_id is always None even if one was passed in."""
        backend = AnthropicAPIBackend()
        handle = await backend.restore_session("sys", [], external_id="some-token")
        assert handle.external_id is None

    async def test_handle_is_anthropic_session_handle(self):
        backend = AnthropicAPIBackend()
        handle = await backend.restore_session("sys", [], external_id=None)
        assert isinstance(handle, AnthropicSessionHandle)


# ---------------------------------------------------------------------------
# 4 — send_message: appends turns correctly
# ---------------------------------------------------------------------------

class TestSendMessageAppendsTurns:
    async def test_handle_has_four_messages_after_send(self):
        # Start with a handle from a prior exchange (2 messages already)
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": FINAL_RAW},
            ],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.send_message(handle, "follow-up question")

        assert len(handle.messages) == 4

    async def test_new_user_turn_appended(self):
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": FINAL_RAW},
            ],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.send_message(handle, "follow-up question")

        assert handle.messages[2] == {"role": "user", "content": "follow-up question"}

    async def test_new_assistant_turn_appended(self):
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": FINAL_RAW},
            ],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.send_message(handle, "follow-up question")

        # assistant turn stores raw response
        assert handle.messages[3] == {"role": "assistant", "content": FINAL_RAW}

    async def test_api_called_with_all_messages_including_new_user_turn(self):
        """Verify the API receives all prior messages plus the new user turn.

        The implementation passes the same list object to _call_api; after the call
        the assistant turn is appended to that same list.  We capture a snapshot
        inside a side_effect to see the messages at the moment the API is called.
        """
        prior_messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": FINAL_RAW},
        ]
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=list(prior_messages),
        )

        captured: list[list[dict]] = []
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=FINAL_RAW)]

        async def capture_messages(*args, **kwargs):
            # Snapshot the messages list at call time (before assistant append)
            captured.append(list(kwargs["messages"]))
            return mock_response

        mock_client = MagicMock()
        mock_client.messages.create = capture_messages
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.send_message(handle, "follow-up question")

        assert len(captured) == 1
        sent_at_call_time = captured[0]
        assert len(sent_at_call_time) == 3
        assert sent_at_call_time[2] == {"role": "user", "content": "follow-up question"}


# ---------------------------------------------------------------------------
# 5 — send_message: API call receives system_prompt
# ---------------------------------------------------------------------------

class TestSendMessageSystemPrompt:
    async def test_api_called_with_system_prompt_kwarg(self):
        """Default prompt_caching=True: system is a cache-annotated block array."""
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="my system prompt",
            messages=[],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.send_message(handle, "user text")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": "my system prompt",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_prompt_caching_false_sends_bare_string(self):
        """prompt_caching=False must be byte-identical to the pre-Phase-6 shape."""
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="my system prompt",
            messages=[],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(prompt_caching=False)
            await backend.send_message(handle, "user text")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "my system prompt"

    async def test_api_called_with_model_kwarg(self):
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(model="claude-opus-4-7")
            await backend.send_message(handle, "user text")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# 6 — end_session: clears messages
# ---------------------------------------------------------------------------

class TestEndSession:
    async def test_messages_cleared(self):
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": FINAL_RAW},
            ],
        )
        backend = AnthropicAPIBackend()
        await backend.end_session(handle)
        assert handle.messages == []

    async def test_end_session_no_api_call(self):
        handle = AnthropicSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[{"role": "user", "content": "q"}],
        )
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.end_session(handle)
        mock_client.messages.create.assert_not_called()


# ---------------------------------------------------------------------------
# 7 — _call_api: timeout raises AgentTimeout
# ---------------------------------------------------------------------------

class TestCallApiTimeout:
    async def test_timeout_raises_agent_timeout(self):
        async def hang(*args, **kwargs):
            await asyncio.sleep(999)

        mock_client = MagicMock()
        mock_client.messages.create = hang
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(timeout=0.01)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "initial message")

    async def test_agent_timeout_message_mentions_timeout_value(self):
        async def hang(*args, **kwargs):
            await asyncio.sleep(999)

        mock_client = MagicMock()
        mock_client.messages.create = hang
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(timeout=0.01)
            with pytest.raises(AgentTimeout, match="0.01"):
                await backend.start_session("sys", "initial message")


# ---------------------------------------------------------------------------
# 8 — restore_session: empty history
# ---------------------------------------------------------------------------

class TestRestoreSessionEmptyHistory:
    async def test_empty_history_produces_empty_messages(self):
        backend = AnthropicAPIBackend()
        handle = await backend.restore_session("sys", [], external_id=None)
        assert handle.messages == []

    async def test_empty_history_no_error(self):
        backend = AnthropicAPIBackend()
        # Should not raise
        handle = await backend.restore_session("sys", [], external_id=None)
        assert handle is not None


# ---------------------------------------------------------------------------
# 9 — Registry: backend_for("anthropic") returns AnthropicAPIBackend
# ---------------------------------------------------------------------------

class TestRegistryAnthropicBackend:
    def test_backend_for_anthropic_returns_anthropic_backend(self):
        from jsa.agents.registry import backend_for
        b = backend_for("anthropic")
        assert isinstance(b, AnthropicAPIBackend)

    def test_backend_for_anthropic_name_attribute(self):
        from jsa.agents.registry import backend_for
        b = backend_for("anthropic")
        assert b.name == "anthropic"

    def test_backend_for_anthropic_creates_new_instance_each_call(self):
        from jsa.agents.registry import backend_for
        b1 = backend_for("anthropic")
        b2 = backend_for("anthropic")
        assert b1 is not b2


# ---------------------------------------------------------------------------
# 10 — Config: new fields have correct defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_model_default(self, monkeypatch):
        monkeypatch.delenv("JSA_MODEL", raising=False)
        from jsa.config import Settings
        s = Settings()
        # Deliberately loose: the default model id churns; pin only that one is set.
        assert s.model
        assert s.model.startswith("claude-")

    def test_anthropic_timeout_default(self, monkeypatch):
        monkeypatch.delenv("JSA_ANTHROPIC_TIMEOUT", raising=False)
        from jsa.config import Settings
        s = Settings()
        assert s.anthropic_timeout == 180.0

    def test_model_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("JSA_MODEL", "claude-haiku-3-7")
        from jsa.config import Settings
        s = Settings()
        assert s.model == "claude-haiku-3-7"

    def test_anthropic_timeout_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("JSA_ANTHROPIC_TIMEOUT", "60.0")
        from jsa.config import Settings
        s = Settings()
        assert s.anthropic_timeout == 60.0


# ---------------------------------------------------------------------------
# Bonus — TypeError for wrong handle type
# ---------------------------------------------------------------------------

class TestTypeErrorOnWrongHandle:
    async def test_send_message_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle
        wrong_handle = SessionHandle(id="bad", external_id=None)
        backend = AnthropicAPIBackend()
        with pytest.raises(TypeError, match="AnthropicSessionHandle"):
            await backend.send_message(wrong_handle, "some text")

    async def test_end_session_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle
        wrong_handle = SessionHandle(id="bad", external_id=None)
        backend = AnthropicAPIBackend()
        with pytest.raises(TypeError, match="AnthropicSessionHandle"):
            await backend.end_session(wrong_handle)


# ---------------------------------------------------------------------------
# 11 — Structured mode: forced tool-use request shape
# ---------------------------------------------------------------------------

CV_SCHEMA = json_schema_for(Stage.cv_adjust)
FIT_SCHEMA = json_schema_for(Stage.fit_assessment)
TOOL_USE_KWARGS = {"tools": [{"name": "respond", "input_schema": CV_SCHEMA}],
                   "tool_choice": {"type": "tool", "name": "respond"}}


def _cv_payload() -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [{"name": "Summary", "text": "Senior engineer."}],
    }


class TestStructuredForcedToolRequest:
    async def test_tools_and_tool_choice_sent_when_schema_given(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": _cv_payload()})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == [{"name": "respond", "input_schema": CV_SCHEMA}]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "respond"}

    async def test_no_tools_kwargs_when_schema_none(self):
        """None-schema → sentinel parity at the wire level: no tools/tool_choice sent."""
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.start_session("sys", "msg")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert "tools" not in call_kwargs
        assert "tool_choice" not in call_kwargs

    async def test_model_max_tokens_system_unchanged_in_structured_mode(self):
        """Structured mode doesn't affect model/max_tokens/caching independently.

        Render order (CLAUDE.md "Prompt caching") is tools -> system -> messages,
        so the one cache_control breakpoint on the system block covers the forced
        tool schema too — no separate breakpoint on the tool definition is needed.
        """
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": _cv_payload()})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(model="claude-opus-4-7")
            await backend.start_session(
                "my system", "msg", structured_schema=CV_SCHEMA
            )
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-4-7"
        assert call_kwargs["max_tokens"] == 32000
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": "my system",
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_prompt_caching_false_bare_string_in_structured_mode(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": _cv_payload()})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(prompt_caching=False)
            await backend.start_session(
                "my system", "msg", structured_schema=CV_SCHEMA
            )
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == "my system"


# ---------------------------------------------------------------------------
# 12 — Structured mode: final reply through the tool_use block
# ---------------------------------------------------------------------------

class TestStructuredFinalReply:
    async def test_kind_final_and_content_is_reserialized_payload(self):
        payload = _cv_payload()
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": payload})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert reply.kind == "final"
        assert json.loads(reply.content) == payload

    async def test_raw_is_canonical_json_not_provider_envelope(self):
        payload = _cv_payload()
        tool_input = {"kind": "final", "question": None, "payload": payload}
        mock_client = _make_mock_block_client([_tool_block(tool_input)])
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        # Canonical-form invariant: raw is bare union-JSON text (persisted verbatim
        # into Message rows), never a tool_use block or provider envelope.
        assert reply.raw.startswith("{")
        assert json.loads(reply.raw) == tool_input

    async def test_handle_assistant_turn_is_canonical_json(self):
        payload = _cv_payload()
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": payload})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assistant_turn = handle.messages[1]
        assert assistant_turn["role"] == "assistant"
        # Starts with "{" — the property the replay adapter's content-based
        # detection (jsa/schema/turn_models.py) keys off.
        assert assistant_turn["content"].startswith("{")

    async def test_handle_structured_schema_stamped(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": _cv_payload()})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert handle.structured_schema == CV_SCHEMA

    async def test_content_validates_downstream_as_cvdocument(self):
        """The structured path's content is byte-compatible with the sentinel path:
        the same CVDocument validation the sentinel path runs succeeds on it."""
        payload = _cv_payload()
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": payload})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert CVDocument.model_validate(json.loads(reply.content)).contact.name == "Jane Doe"


# ---------------------------------------------------------------------------
# 13 — Structured mode: question reply
# ---------------------------------------------------------------------------

class TestStructuredQuestionReply:
    async def test_kind_needs_input_and_question_populated(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "question", "question": "Which dates?", "payload": None})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert reply.kind == "needs_input"
        assert reply.question == "Which dates?"
        assert reply.content == "Which dates?"


# ---------------------------------------------------------------------------
# 14 — Structured mode: tool_use block is scanned, not indexed at content[0]
# ---------------------------------------------------------------------------

class TestStructuredScanNotFirstBlock:
    async def test_text_block_before_tool_block_still_extracted(self):
        """A model under forced tool-use may emit preamble text before the tool
        call — extraction must scan for the tool_use block, not read content[0]."""
        mock_client = _make_mock_block_client(
            [_text_block("Let me look at the JD first..."), _tool_block(
                {"kind": "final", "question": None, "payload": _cv_payload()}
            )]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert reply.kind == "final"
        assert json.loads(reply.content) == _cv_payload()


# ---------------------------------------------------------------------------
# 15 — Structured mode: truncation + missing tool_use block
# ---------------------------------------------------------------------------

class TestStructuredTruncation:
    async def test_max_tokens_stop_reason_raises_truncation_protocol_error(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final"})], stop_reason="max_tokens"
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="truncated"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_truncation_checked_before_extraction(self):
        """stop_reason is checked BEFORE scanning content: even an empty block
        list with max_tokens reports truncation, not 'no tool_use block'."""
        mock_client = _make_mock_block_client([], stop_reason="max_tokens")
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="truncated"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_truncated_reply_not_appended_to_handle(self):
        """send_message mutates handle.messages only after success — a truncation
        ProtocolError leaves the conversation list coherent."""
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final"})], stop_reason="max_tokens"
        )
        handle = AnthropicSessionHandle(
            id="t", external_id=None, system_prompt="sys",
            messages=[{"role": "user", "content": "first"}],
            structured_schema=CV_SCHEMA,
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="truncated"):
                await backend.send_message(handle, "follow-up")
        assert handle.messages == [{"role": "user", "content": "first"}]

    async def test_no_tool_use_block_raises_protocol_error(self):
        mock_client = _make_mock_block_client(
            [_text_block("I cannot answer that.")], stop_reason="end_turn"
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="no tool_use block"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)


# ---------------------------------------------------------------------------
# 16 — Structured mode: model violated its own tool schema (parse-level errors)
# ---------------------------------------------------------------------------

class TestStructuredParseErrorsFromToolInput:
    async def test_invalid_kind_in_tool_input(self):
        mock_client = _make_mock_block_client([_tool_block({"kind": "bogus"})])
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="kind"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_missing_kind_in_tool_input(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"question": None, "payload": _cv_payload()})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="kind"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_final_without_payload_in_tool_input(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": None})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="payload"):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)


# ---------------------------------------------------------------------------
# 17 — Structured mode: fit verdict (schema-keyed routing — no Stage in the backend)
# ---------------------------------------------------------------------------

class TestStructuredFitVerdict:
    async def test_fit_verdict_content_reuses_fit_parser_shape(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"verdict": "FIT", "reason": "JD and profile align on backend depth"})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=FIT_SCHEMA
            )
        assert reply.kind == "final"
        # The exact "VERDICT\nreason" shape _parse_fit_verdict (stages.py) consumes.
        assert reply.content == "FIT\nJD and profile align on backend depth"

    async def test_unfit_verdict_round_trips(self):
        mock_client = _make_mock_block_client(
            [_tool_block({"verdict": "UNFIT", "reason": "requires 15+ years, candidate has 4"})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            _, reply = await backend.start_session(
                "sys", "msg", structured_schema=FIT_SCHEMA
            )
        assert reply.content == "UNFIT\nrequires 15+ years, candidate has 4"

    async def test_missing_reason_in_tool_input_rejected(self):
        mock_client = _make_mock_block_client([_tool_block({"verdict": "FIT"})])
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(ProtocolError, match="reason"):
                await backend.start_session(
                    "sys", "msg", structured_schema=FIT_SCHEMA
                )


# ---------------------------------------------------------------------------
# 18 — Structured mode: the handle carries the session's schema
# ---------------------------------------------------------------------------

class TestStructuredHandleCarriesSchema:
    async def test_send_message_without_kwarg_stays_structured(self):
        """The session's mode is established at start_session and carried on the
        handle — follow-up send_message calls keep forced tool-use with no kwarg."""
        payload = _cv_payload()
        # Two separate clients: start_session reply, then the send_message reply.
        first = _make_mock_block_client(
            [_tool_block({"kind": "question", "question": "Which dates?", "payload": None})]
        )
        second = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": payload})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=first):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
        assert reply.kind == "needs_input"
        with patch("anthropic.AsyncAnthropic", return_value=second):
            reply2 = await backend.send_message(handle, "2020-2023")
        assert reply2.kind == "final"
        assert json.loads(reply2.content) == payload
        call_kwargs = second.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == [{"name": "respond", "input_schema": CV_SCHEMA}]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "respond"}

    async def test_restore_session_stamps_schema(self):
        backend = AnthropicAPIBackend()
        history = [HistoryTurn(role="user", content="q")]
        handle = await backend.restore_session(
            "sys", history, external_id=None, structured_schema=CV_SCHEMA
        )
        assert handle.structured_schema == CV_SCHEMA

    async def test_restored_structured_session_send_message_forces_tool(self):
        first = _make_mock_client(FINAL_RAW)  # only used to have a patchable client
        second = _make_mock_block_client(
            [_tool_block({"kind": "final", "question": None, "payload": _cv_payload()})]
        )
        backend = AnthropicAPIBackend()
        history = [
            HistoryTurn(role="user", content="q"),
            HistoryTurn(role="assistant", content='{"kind": "question", "question": "Q?", "payload": None}'),
        ]
        handle = await backend.restore_session(
            "sys", history, external_id=None, structured_schema=CV_SCHEMA
        )
        with patch("anthropic.AsyncAnthropic", return_value=second):
            reply = await backend.send_message(handle, "the answer")
        assert reply.kind == "final"
        assert "tools" in second.messages.create.call_args.kwargs

    async def test_restore_session_without_schema_defaults_sentinel(self):
        mock_client = _make_mock_client(FINAL_RAW)
        backend = AnthropicAPIBackend()
        handle = await backend.restore_session("sys", [], external_id=None)
        assert handle.structured_schema is None
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            reply = await backend.send_message(handle, "hello")
        assert reply.kind == "final"
        assert "tools" not in mock_client.messages.create.call_args.kwargs

    async def test_explicit_kwarg_overrides_handle_schema(self):
        """The documented override branch: a non-None kwarg wins over the handle's
        schema for that call, while the handle's own schema is left untouched."""
        payload = {"verdict": "FIT", "reason": "aligns on backend depth"}
        mock_client = _make_mock_block_client([_tool_block(payload)])
        handle = AnthropicSessionHandle(
            id="t", external_id=None, system_prompt="sys",
            messages=[{"role": "user", "content": "q"}],
            structured_schema=CV_SCHEMA,
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            reply = await backend.send_message(
                handle, "text", structured_schema=FIT_SCHEMA
            )
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == [{"name": "respond", "input_schema": FIT_SCHEMA}]
        assert reply.kind == "final"
        assert reply.content == "FIT\naligns on backend depth"
        # The handle's session mode itself is not mutated by a one-call override.
        assert handle.structured_schema == CV_SCHEMA


# ---------------------------------------------------------------------------
# 19 — Structured mode: exception mapping untouched
# ---------------------------------------------------------------------------

class TestStructuredExceptionMapping:
    async def test_timeout_raises_agent_timeout_in_structured_mode(self):
        async def hang(*args, **kwargs):
            await asyncio.sleep(999)

        mock_client = MagicMock()
        mock_client.messages.create = hang
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(timeout=0.01)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_rate_limit_raises_agent_limit_reached_in_structured_mode(self):
        import anthropic

        rate_limit_error = anthropic.RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(),
            body={},
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)


# ---------------------------------------------------------------------------
# 20 — Structured-mode capability flag
# ---------------------------------------------------------------------------

class TestSupportsStructuredOutput:
    def test_class_flag_is_true(self):
        assert AnthropicAPIBackend.supports_structured_output is True

    def test_registry_instance_flag_is_true(self):
        from jsa.agents.registry import backend_for
        assert backend_for("anthropic").supports_structured_output is True


# ---------------------------------------------------------------------------
# Phase 6 — prompt caching: ctor default, factory forwarding, cached-token
# observability. Payload-shape and kill-switch-parity coverage for
# prompt_caching=True/False already lives in TestSendMessageSystemPrompt and
# TestStructuredForcedToolRequest above (mirroring where CLAUDE.md's Phase 6
# section says to look).
# ---------------------------------------------------------------------------

class TestPromptCachingCtorDefault:
    def test_prompt_caching_defaults_true(self):
        assert AnthropicAPIBackend()._prompt_caching is True

    def test_prompt_caching_overridable(self):
        assert AnthropicAPIBackend(prompt_caching=False)._prompt_caching is False


class TestPromptCachingFactoryForwarding:
    def test_backend_for_forwards_prompt_caching_true(self):
        from jsa.agents.registry import backend_for
        backend = backend_for("anthropic", prompt_caching=True)
        assert backend._prompt_caching is True

    def test_backend_for_forwards_prompt_caching_false(self):
        from jsa.agents.registry import backend_for
        backend = backend_for("anthropic", prompt_caching=False)
        assert backend._prompt_caching is False


class TestCachedTokenObservability:
    async def test_cache_usage_logged_when_present(self, caplog):
        import logging as _logging

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=FINAL_RAW)]
        mock_response.usage.cache_read_input_tokens = 1234
        mock_response.usage.cache_creation_input_tokens = 56
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with caplog.at_level(_logging.INFO, logger="jsa.agents.anthropic_api"):
                backend = AnthropicAPIBackend()
                await backend.start_session("sys", "hi")

        assert "cache_read_input_tokens=1234" in caplog.text
        assert "cache_creation_input_tokens=56" in caplog.text

    async def test_missing_usage_does_not_raise(self):
        """A response whose usage/token fields aren't real ints must not crash."""
        mock_client = _make_mock_client(FINAL_RAW)  # plain MagicMock().usage
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"


# ---------------------------------------------------------------------------
# Phase 3 / E1 — native tool mode (revision-tool-use plan)
#
# Sentinel/structured coverage above is untouched; everything below exercises the
# third channel: a session established with `tools=` (tool_loop.py's native rung).
# ---------------------------------------------------------------------------

REV_SPECS = tools_for(Stage.revising_cv)
REV_CL_SPECS = tools_for(Stage.revising_cl)


def _tool_use_block(call_id: str, name: str, tool_input: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = call_id
    block.name = name
    block.input = tool_input
    return block


def _bad_request_error() -> Exception:
    import anthropic

    response = MagicMock()
    response.status_code = 400
    return anthropic.BadRequestError(
        message="tools: unsupported", response=response, body={}
    )


async def _tool_session(backend: AnthropicAPIBackend, specs=REV_SPECS):
    """A restored native-tool session — the shape tool_loop.py's native rung builds."""
    return await backend.restore_session(
        "tool contract system prompt",
        [HistoryTurn(role="user", content="original revision request")],
        None,
        tools=specs,
    )


class TestSupportsNativeTools:
    def test_class_flag_is_true(self):
        assert AnthropicAPIBackend.supports_native_tools is True

    def test_registry_instance_flag_is_true(self):
        from jsa.agents.registry import backend_for
        assert backend_for("anthropic").supports_native_tools is True


class TestNativeToolRequestShape:
    async def test_tools_rendered_by_to_anthropic_tools_and_tool_choice_any(self):
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_01", "get_cv", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "make it shorter")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == to_anthropic_tools(REV_SPECS)
        assert call_kwargs["tool_choice"] == {"type": "any"}

    async def test_no_structured_respond_tool_in_tool_mode(self):
        """The terminal tool's arguments ARE the structured output — no `respond`."""
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_01", "get_cv", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "make it shorter")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert all(tool["name"] != "respond" for tool in call_kwargs["tools"])
        assert call_kwargs["tool_choice"] != {"type": "tool", "name": "respond"}
        assert "response_format" not in call_kwargs

    async def test_restore_session_stamps_tools_on_handle(self):
        backend = AnthropicAPIBackend()
        handle = await _tool_session(backend)
        assert handle.tools == REV_SPECS
        assert handle.structured_schema is None

    async def test_tools_and_schema_together_rejected(self):
        backend = AnthropicAPIBackend()
        with pytest.raises(ValueError):
            await backend.restore_session(
                "sys", [], None, structured_schema=CV_SCHEMA, tools=REV_SPECS
            )

    async def test_send_message_rejects_schema_on_a_tool_session(self):
        backend = AnthropicAPIBackend()
        handle = await _tool_session(backend)
        with pytest.raises(ValueError):
            await backend.send_message(handle, "hi", structured_schema=CV_SCHEMA)

    async def test_cl_stage_vocabulary_rendered_too(self):
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_01", "get_letter", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend, REV_CL_SPECS)
            await backend.send_message(handle, "tighten it")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == to_anthropic_tools(REV_CL_SPECS)

    async def test_prompt_caching_still_one_system_breakpoint(self):
        """Render order is tools -> system -> messages, so the existing system
        breakpoint covers the tool definitions; no second cache_control appears."""
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_01", "get_cv", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "go")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["system"] == [
            {
                "type": "text",
                "text": "tool contract system prompt",
                "cache_control": {"type": "ephemeral"},
            }
        ]
        assert all("cache_control" not in tool for tool in call_kwargs["tools"])

    async def test_tool_mode_never_streams(self):
        """on_chunk is dropped on the tool path (plan Phase 2 finding #1)."""
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_01", "get_cv", {})]
        )
        mock_client.messages.stream = MagicMock(
            side_effect=AssertionError("tool mode must not stream")
        )
        chunks = []

        async def on_chunk(chunk):
            chunks.append(chunk)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go", on_chunk=on_chunk)
        assert reply.kind == "tool_calls"
        assert chunks == []


class TestNativeToolCallExtraction:
    async def test_all_tool_use_blocks_returned_in_order(self):
        mock_client = _make_mock_block_client(
            [
                _text_block("Here goes."),
                _tool_use_block("toolu_a", "edit_entry_bullets",
                                {"entry_id": "e1", "bullets": ["one"]}),
                _tool_use_block("toolu_b", "replace_summary", {"text": "Shorter."}),
                _tool_use_block("toolu_c", "finalize", {"change_log": "done"}),
            ]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go")
        assert reply.kind == "tool_calls"
        assert [c.name for c in reply.tool_calls] == [
            "edit_entry_bullets", "replace_summary", "finalize"
        ]
        assert [c.id for c in reply.tool_calls] == ["toolu_a", "toolu_b", "toolu_c"]
        assert reply.tool_calls[0].arguments == {"entry_id": "e1", "bullets": ["one"]}

    async def test_raw_and_content_are_the_text_blocks_not_the_envelope(self):
        mock_client = _make_mock_block_client(
            [_text_block("Thinking out loud."),
             _tool_use_block("toolu_a", "get_cv", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go")
        assert reply.raw == "Thinking out loud."
        assert reply.content == "Thinking out loud."
        assert "tool_use" not in reply.raw

    async def test_max_tokens_stop_reason_raises_protocol_error(self):
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_a", "get_cv", {})], stop_reason="max_tokens"
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            with pytest.raises(ProtocolError):
                await backend.send_message(handle, "go")

    async def test_truncated_reply_not_appended_to_handle(self):
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_a", "get_cv", {})], stop_reason="max_tokens"
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            before = list(handle.messages)
            with pytest.raises(ProtocolError):
                await backend.send_message(handle, "go")
        assert handle.messages == before

    async def test_no_tool_use_block_returns_non_tool_calls_reply(self):
        """The loop's _no_parseable_call case — a reply, never a raised error."""
        mock_client = _make_mock_block_client(
            [_text_block("I would rather just explain myself.")], stop_reason="end_turn"
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go")
        assert reply.kind != "tool_calls"
        assert reply.tool_calls is None
        assert reply.content == "I would rather just explain myself."

    async def test_assistant_tool_use_turn_appended_to_handle(self):
        mock_client = _make_mock_block_client(
            [_tool_use_block("toolu_a", "get_cv", {})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "go")
        assert handle.messages[-2] == {"role": "user", "content": "go"}
        assert handle.messages[-1] == {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_a", "name": "get_cv", "input": {}}
            ],
        }


class TestSendToolResults:
    async def test_tool_result_blocks_echo_the_call_ids(self):
        first = _make_mock_block_client(
            [_tool_use_block("toolu_a", "get_cv", {}),
             _tool_use_block("toolu_b", "replace_summary", {"text": "Hi."})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=first):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go")

        second = _make_mock_block_client(
            [_tool_use_block("toolu_c", "finalize", {"change_log": "ok"})]
        )
        results = [
            ToolResult(call_id=c.id, name=c.name, ok=True, content={"ok": True})
            for c in reply.tool_calls
        ]
        with patch("anthropic.AsyncAnthropic", return_value=second):
            follow_up = await backend.send_tool_results(handle, results)

        sent = second.messages.create.call_args.kwargs["messages"]
        result_turn = sent[-1]
        assert result_turn["role"] == "user"
        assert [b["tool_use_id"] for b in result_turn["content"]] == ["toolu_a", "toolu_b"]
        assert all(b["type"] == "tool_result" for b in result_turn["content"])
        assert json.loads(result_turn["content"][0]["content"]) == {"ok": True}
        assert follow_up.kind == "tool_calls"
        assert follow_up.tool_calls[0].name == "finalize"

    async def test_tools_still_attached_on_the_follow_up_call(self):
        first = _make_mock_block_client([_tool_use_block("toolu_a", "get_cv", {})])
        with patch("anthropic.AsyncAnthropic", return_value=first):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            reply = await backend.send_message(handle, "go")

        second = _make_mock_block_client(
            [_tool_use_block("toolu_b", "finalize", {"change_log": "ok"})]
        )
        with patch("anthropic.AsyncAnthropic", return_value=second):
            await backend.send_tool_results(
                handle,
                [ToolResult(call_id="toolu_a", name="get_cv", ok=True, content={"ok": True})],
            )
        call_kwargs = second.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == to_anthropic_tools(REV_SPECS)
        assert call_kwargs["tool_choice"] == {"type": "any"}

    async def test_assistant_tool_use_turn_precedes_the_results(self):
        first = _make_mock_block_client([_tool_use_block("toolu_a", "get_cv", {})])
        with patch("anthropic.AsyncAnthropic", return_value=first):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "go")

        second = _make_mock_block_client([_text_block("no more tools")])
        with patch("anthropic.AsyncAnthropic", return_value=second):
            await backend.send_tool_results(
                handle,
                [ToolResult(call_id="toolu_a", name="get_cv", ok=True, content={"ok": True})],
            )
        sent = second.messages.create.call_args.kwargs["messages"]
        assert sent[-2]["role"] == "assistant"
        assert sent[-2]["content"][0]["type"] == "tool_use"
        assert sent[-1]["role"] == "user"
        assert sent[-1]["content"][0]["type"] == "tool_result"

    async def test_wrong_handle_type_raises_type_error(self):
        backend = AnthropicAPIBackend()
        with pytest.raises(TypeError):
            await backend.send_tool_results(SessionHandle(id="x"), [])

    async def test_handle_not_mutated_when_the_call_fails(self):
        first = _make_mock_block_client([_tool_use_block("toolu_a", "get_cv", {})])
        with patch("anthropic.AsyncAnthropic", return_value=first):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            await backend.send_message(handle, "go")
        before = list(handle.messages)

        failing = MagicMock()
        failing.messages.create = AsyncMock(side_effect=_bad_request_error())
        failing.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=failing):
            with pytest.raises(ToolsUnsupported):
                await backend.send_tool_results(
                    handle,
                    [ToolResult(call_id="toolu_a", name="get_cv", ok=True, content={"ok": True})],
                )
        assert handle.messages == before


class TestToolsRejectedClassification:
    async def test_bad_request_with_native_tools_raises_tools_rejected(self):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_bad_request_error())
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            with pytest.raises(_ToolsRejected):
                await backend.send_message(handle, "go")

    def test_tools_rejected_is_a_tools_unsupported(self):
        assert issubclass(_ToolsRejected, ToolsUnsupported)

    def test_tools_rejected_is_not_a_backend_unavailable(self):
        """Subclassing AgentBackendUnavailable would make BF-19 advance the whole job
        to the next backend over a tools-only degrade — the loss the rung ladder
        exists to prevent (see the class docstring)."""
        assert not issubclass(_ToolsRejected, AgentBackendUnavailable)

    async def test_tool_loop_catches_it_as_tools_unsupported(self):
        """The contract that matters: tool_loop.py's `except ToolsUnsupported`."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_bad_request_error())
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            caught = False
            try:
                await backend.send_message(handle, "go")
            except ToolsUnsupported:
                caught = True
            assert caught

    async def test_bad_request_in_structured_mode_is_backend_unavailable(self):
        """Structured mode also sends tools/tool_choice, but has no rung ladder to
        catch a ToolsUnsupported — it must stay BF-19-recognized."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_bad_request_error())
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentBackendUnavailable) as excinfo:
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert not isinstance(excinfo.value, ToolsUnsupported)

    async def test_bad_request_with_no_tools_at_all_is_backend_unavailable(self):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=_bad_request_error())
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentBackendUnavailable) as excinfo:
                await backend.start_session("sys", "msg")
        assert not isinstance(excinfo.value, ToolsUnsupported)

    async def test_rate_limit_still_wins_over_bad_request_in_tool_mode(self):
        import anthropic

        rate_limit_error = anthropic.RateLimitError(
            message="Rate limit exceeded", response=MagicMock(), body={}
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=rate_limit_error)
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle = await _tool_session(backend)
            with pytest.raises(AgentLimitReached):
                await backend.send_message(handle, "go")

    async def test_timeout_still_wins_in_tool_mode(self):
        async def hang(*args, **kwargs):
            await asyncio.sleep(999)

        mock_client = MagicMock()
        mock_client.messages.create = hang
        mock_client.close = AsyncMock()
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(timeout=0.01)
            handle = await _tool_session(backend)
            with pytest.raises(AgentTimeout):
                await backend.send_message(handle, "go")

    async def test_send_tool_results_on_a_non_tool_session_is_a_caller_error(self):
        """Not a _ToolsRejected: a tool-less handle is a caller bug, and misreporting
        it as a provider tools rejection would silently downgrade the rung."""
        backend = AnthropicAPIBackend()
        handle = await backend.restore_session("sys", [], None)
        with pytest.raises(ValueError):
            await backend.send_tool_results(
                handle,
                [ToolResult(call_id="toolu_a", name="get_cv", ok=True, content={"ok": True})],
            )
