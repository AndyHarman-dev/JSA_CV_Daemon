"""Tests for jsa/agents/anthropic_api.py: AnthropicAPIBackend and AnthropicSessionHandle.

The `import anthropic` occurs *inside* `_call_api`, not at module scope, so we patch
`anthropic.AsyncAnthropic` directly on the SDK module rather than at the file attribute.

All tests are async; asyncio_mode = "auto" is set in pyproject.toml.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx
import pytest

from jsa.agents.anthropic_api import AnthropicAPIBackend, AnthropicSessionHandle, require_api_key
from jsa.agents.base import (
    AgentLimitReached,
    AgentOutputTruncated,
    AgentRequestError,
    AgentTimeout,
    HistoryTurn,
)


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


class _FakeTextBlock:
    """Plain (non-MagicMock) content block with a .text attribute.

    Used instead of MagicMock for the content-guard tests below, since a bare
    MagicMock auto-vivifies any attribute accessed on it (hasattr(mock, "text")
    is always True), which would silently defeat the guard we're testing.
    """

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeNonTextBlock:
    """Content block with no .text attribute — e.g. a non-text block type."""

    def __init__(self, block_type: str = "thinking") -> None:
        self.type = block_type


class _FakeResponse:
    """Plain response stub carrying only what _call_api reads: .content and .stop_reason."""

    def __init__(self, *, content: list, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


def _api_status_error(cls: type, status_code: int, message: str = "error") -> Exception:
    """Construct a real anthropic.APIStatusError subclass instance for exception-mapping tests."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=None)


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
        """system is now a content-block list (prompt caching, U4) rather than
        a bare string — see TestPromptCaching below for the dedicated checks."""
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
        assert s.model == "claude-opus-4-7"

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
# 11 — U4: connection reuse (single shared client across calls)
# ---------------------------------------------------------------------------

class TestConnectionReuse:
    async def test_client_constructed_once_across_two_calls(self):
        """AsyncAnthropic() must be constructed at most once per backend
        instance, not once per _call_api invocation."""
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client) as ctor:
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("sys", "first message")
            await backend.send_message(handle, "second message")

        assert ctor.call_count == 1

    async def test_client_not_closed_after_call(self):
        """The shared client must survive past a single _call_api — no more
        per-call `await client.close()` in a finally block."""
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.start_session("sys", "message")

        mock_client.close.assert_not_called()

    async def test_same_client_instance_reused(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            client_a = backend._get_client()
            client_b = backend._get_client()
        assert client_a is client_b


# ---------------------------------------------------------------------------
# 12 — U4: max_tokens wiring
# ---------------------------------------------------------------------------

class TestMaxTokensWiring:
    async def test_default_max_tokens_sent_to_create(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.start_session("sys", "msg")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 16384

    async def test_custom_max_tokens_sent_to_create(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(max_tokens=32000)
            await backend.start_session("sys", "msg")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["max_tokens"] == 32000

    def test_config_default(self, monkeypatch):
        monkeypatch.delenv("JSA_MAX_TOKENS", raising=False)
        from jsa.config import Settings
        s = Settings()
        assert s.max_tokens == 16384

    def test_config_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("JSA_MAX_TOKENS", "32000")
        from jsa.config import Settings
        s = Settings()
        assert s.max_tokens == 32000


# ---------------------------------------------------------------------------
# 13 — U4: prompt caching — system content-block structure
# ---------------------------------------------------------------------------

class TestPromptCaching:
    async def test_system_is_content_block_list_with_cache_control(self):
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            await backend.start_session("my large stable system prompt", "msg")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert isinstance(system, list)
        assert len(system) == 1
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "my large stable system prompt"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    async def test_cache_breakpoint_present_on_every_turn(self):
        """The cache_control breakpoint must be on the system block for every
        turn (start_session AND send_message), not just the first call —
        otherwise resumed/follow-up turns never hit the cache."""
        mock_client = _make_mock_client(FINAL_RAW)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, _ = await backend.start_session("sys prompt", "first")
            await backend.send_message(handle, "second")

        second_call_kwargs = mock_client.messages.create.call_args.kwargs
        assert second_call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# 14 — U4: stop_reason == "max_tokens" handling
# ---------------------------------------------------------------------------

class TestStopReasonMaxTokens:
    async def test_truncated_output_raises_agent_output_truncated(self):
        fake_response = _FakeResponse(
            content=[_FakeTextBlock("partial, truncated content")],
            stop_reason="max_tokens",
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentOutputTruncated):
                await backend.start_session("sys", "msg")

    async def test_truncated_error_message_mentions_max_tokens(self):
        fake_response = _FakeResponse(
            content=[_FakeTextBlock("partial")], stop_reason="max_tokens"
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend(max_tokens=4096)
            with pytest.raises(AgentOutputTruncated, match="4096"):
                await backend.start_session("sys", "msg")

    async def test_non_truncated_stop_reason_does_not_raise(self):
        fake_response = _FakeResponse(
            content=[_FakeTextBlock(FINAL_RAW)], stop_reason="end_turn"
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session("sys", "msg")
        assert reply.kind == "final"


# ---------------------------------------------------------------------------
# 15 — U4: content guard (empty content list / missing .text)
# ---------------------------------------------------------------------------

class TestContentGuard:
    async def test_empty_content_list_raises_agent_request_error(self):
        fake_response = _FakeResponse(content=[], stop_reason="end_turn")
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentRequestError):
                await backend.start_session("sys", "msg")

    async def test_block_without_text_raises_agent_request_error(self):
        fake_response = _FakeResponse(
            content=[_FakeNonTextBlock("thinking")], stop_reason="end_turn"
        )
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=fake_response)
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentRequestError, match="thinking"):
                await backend.start_session("sys", "msg")


# ---------------------------------------------------------------------------
# 16 — U4: exception mapping
# ---------------------------------------------------------------------------

class TestExceptionMapping:
    async def test_bad_request_error_maps_to_agent_request_error(self):
        exc = _api_status_error(anthropic.BadRequestError, 400, "bad max_tokens")

        async def raise_bad_request(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_bad_request
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentRequestError):
                await backend.start_session("sys", "msg")

    async def test_rate_limit_error_maps_to_agent_limit_reached(self):
        exc = _api_status_error(anthropic.RateLimitError, 429, "rate limited")

        async def raise_rate_limit(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_rate_limit
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg")

    async def test_api_timeout_error_maps_to_agent_timeout(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.APITimeoutError(request=request)

        async def raise_timeout(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_timeout
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_api_connection_error_maps_to_agent_timeout(self):
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        exc = anthropic.APIConnectionError(request=request)

        async def raise_connection_error(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_connection_error
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_internal_server_error_maps_to_agent_timeout(self):
        exc = _api_status_error(anthropic.InternalServerError, 500, "server error")

        async def raise_server_error(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_server_error
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_overloaded_error_maps_to_agent_timeout(self):
        """529 overloaded_error — a real 5xx APIStatusError subclass, reached
        via the generic status_code >= 500 branch rather than a specific
        `except` clause (the class isn't part of anthropic's public API)."""
        exc = _api_status_error(anthropic.APIStatusError, 529, "overloaded")

        async def raise_overloaded(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_overloaded
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_permission_denied_error_maps_to_agent_request_error(self):
        """A non-retryable 4xx that isn't BadRequestError/RateLimitError still
        falls into the non-retryable bucket via the generic APIStatusError
        branch, not AgentTimeout."""
        exc = _api_status_error(anthropic.PermissionDeniedError, 403, "forbidden")

        async def raise_permission_denied(*args, **kwargs):
            raise exc

        mock_client = MagicMock()
        mock_client.messages.create = raise_permission_denied
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            with pytest.raises(AgentRequestError):
                await backend.start_session("sys", "msg")


# ---------------------------------------------------------------------------
# 17 — U4: require_api_key() startup validation
# ---------------------------------------------------------------------------

class TestRequireApiKey:
    def test_raises_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            require_api_key()

    def test_no_raise_when_key_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        require_api_key()  # should not raise


# ---------------------------------------------------------------------------
# 18 — U4: server.make_backend_factory wires max_tokens and gates on the key
# ---------------------------------------------------------------------------

class TestServerFactoryAnthropicWiring:
    def test_missing_key_raises_before_constructing_backend(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from jsa.config import Settings
        from jsa.server import make_backend_factory

        factory = make_backend_factory(Settings())
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            factory("anthropic")

    def test_max_tokens_threaded_from_settings(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        monkeypatch.setenv("JSA_MAX_TOKENS", "5000")
        from jsa.config import Settings
        from jsa.server import make_backend_factory

        factory = make_backend_factory(Settings())
        backend = factory("anthropic")
        assert isinstance(backend, AnthropicAPIBackend)
        assert backend._max_tokens == 5000
