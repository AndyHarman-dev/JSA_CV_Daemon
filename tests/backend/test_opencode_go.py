"""Tests for jsa/agents/opencode_go.py's OpenCodeGoBackend.

Covers what's specific to this backend: protocol dispatch (chat vs messages),
the unknown-model ValueError guard, instance-level supports_structured_output,
and the /messages (Anthropic-shape) request/parse/error-classification path. The
/chat/completions half reuses OpenAICompatBackend verbatim — see
tests/backend/test_openai_compat.py for that machinery's thorough coverage;
this file only smoke-tests that the chat path is reachable end-to-end here too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import OpenAICompatSessionHandle
from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached, AgentTimeout, HistoryTurn
from jsa.agents.opencode_go import OpenCodeGoBackend, _PROTOCOL
from jsa.agents.protocol import ProtocolError


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


def _chat_completion_body(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _messages_body(text: str) -> dict:
    return {"type": "message", "content": [{"type": "text", "text": text}]}


def _make_mock_client(json_body: dict, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json = MagicMock(return_value=json_body)
    mock_response.text = str(json_body)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"
NO_SENTINEL_RAW = "Shall I proceed, or would you like to adjust anything?"


class TestProtocolDispatch:
    def test_default_model_is_chat(self):
        backend = OpenCodeGoBackend()
        assert backend._protocol == "chat"
        assert backend.supports_structured_output is True

    def test_messages_model_is_messages(self):
        backend = OpenCodeGoBackend(model="qwen3.8-max")
        assert backend._protocol == "messages"
        assert backend.supports_structured_output is False

    def test_unknown_model_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown OpenCode Go model"):
            OpenCodeGoBackend(model="not-a-real-model")

    def test_every_protocol_entry_is_chat_or_messages(self):
        assert set(_PROTOCOL.values()) <= {"chat", "messages"}

    def test_default_model_in_protocol_table(self):
        assert OpenCodeGoBackend.default_model in _PROTOCOL


class TestChatProtocolEndToEnd:
    async def test_final_reply_via_chat_completions_endpoint(self):
        mock_client = _make_mock_client(_chat_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend()  # glm-5.3, chat
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert isinstance(handle, OpenAICompatSessionHandle)
        assert mock_client.post.call_args.args[0] == "https://opencode.ai/zen/go/v1/chat/completions"

    async def test_env_var_fallback_order(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
        monkeypatch.setenv("OPENCODE_API_KEY", "fallback-key")
        mock_client = _make_mock_client(_chat_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend()
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer fallback-key"

    async def test_go_specific_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
        monkeypatch.setenv("OPENCODE_API_KEY", "zen-key")
        mock_client = _make_mock_client(_chat_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend()
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer go-key"


class TestMessagesProtocolEndToEnd:
    async def test_final_reply_via_messages_endpoint(self):
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.call_args.args[0] == "https://opencode.ai/zen/go/v1/messages"

    async def test_x_api_key_auth_header_used_not_bearer(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "secret")
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["x-api-key"] == "secret"
        assert "Authorization" not in headers

    async def test_system_prompt_is_top_level_not_in_messages_array(self):
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            await backend.start_session("my system prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["system"] == [
            {"type": "text", "text": "my system prompt", "cache_control": {"type": "ephemeral"}}
        ]
        assert all(m["role"] != "system" for m in payload["messages"])

    async def test_system_prompt_stays_bare_string_when_prompt_caching_disabled(self):
        """Phase 5's cache_control breakpoint is speculative (this gateway
        documents no cache API) and gated on the kill switch — with it off, the
        payload must stay byte-identical to the pre-Phase-5 shape."""
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max", prompt_caching=False)
            await backend.start_session("my system prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["system"] == "my system prompt"
        assert all(m["role"] != "system" for m in payload["messages"])

    async def test_no_structured_schema_ever_sent(self):
        """The messages protocol is sentinel-only — even if a caller passed a
        schema (it shouldn't, since supports_structured_output is False for this
        instance), the payload never carries response_format."""
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "response_format" not in payload

    async def test_nudge_on_missing_sentinel(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_messages_body(NO_SENTINEL_RAW)), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_messages_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 2

    async def test_missing_sentinel_twice_propagates_protocol_error(self):
        mock_client = _make_mock_client(_messages_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(ProtocolError):
                await backend.start_session("sys", "hi")

    async def test_send_message_appends_history(self):
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            handle, _ = await backend.start_session("sys", "first")
            await backend.send_message(handle, "second")
        assert len(handle.messages) == 4

    async def test_restore_session_no_api_call(self):
        with patch("httpx.AsyncClient") as mock_ctor:
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            handle = await backend.restore_session(
                "sys", [HistoryTurn(role="user", content="hi")], external_id=None
            )
        mock_ctor.assert_not_called()
        assert handle.messages == [{"role": "user", "content": "hi"}]

    async def test_timeout_raises_agent_timeout(self):
        import httpx as httpx_module

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "hi")

    async def test_429_raises_agent_limit_reached(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_anthropic_shape_error_envelope_4xx_raises_backend_unavailable(self):
        mock_client = _make_mock_client(
            {"type": "error", "error": {"type": "invalid_request_error", "message": "bad model"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")

    async def test_anthropic_shape_error_envelope_5xx_retries_then_unavailable(self):
        mock_client = _make_mock_client(
            {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
            status_code=529,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 3

    async def test_rate_limit_error_type_raises_agent_limit_reached(self):
        mock_client = _make_mock_client(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "too many requests"}},
            status_code=429,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_end_session_clears_messages(self):
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            handle, _ = await backend.start_session("sys", "hi")
            await backend.end_session(handle)
        assert handle.messages == []

    async def test_send_message_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle

        backend = OpenCodeGoBackend(model="qwen3.8-max")
        with pytest.raises(TypeError, match="OpenAICompatSessionHandle"):
            await backend.send_message(SessionHandle(id="x"), "hi")


def _messages_error_body(message: str = "no eligible provider") -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


class TestPromptCacheControlChat:
    """Phase 5 of the prompt-caching plan: the /chat/completions protocol reuses
    OpenAICompatBackend's _system_content hook exactly as OpenRouter does, so it
    inherits the Phase 4 _CacheRejected degrade-on-4xx from the shared base for
    free — this class only smoke-tests that OpenCodeGoBackend actually wires the
    override in (the degrade mechanism itself is covered once, thoroughly, in
    test_openrouter.py::TestPromptCacheControl)."""

    async def test_cache_control_sent_by_default(self):
        mock_client = _make_mock_client(_chat_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend()  # glm-5.3, chat
            await backend.start_session("sys prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["messages"][0] == {
            "role": "system",
            "content": [
                {"type": "text", "text": "sys prompt", "cache_control": {"type": "ephemeral"}}
            ],
        }

    async def test_prompt_caching_false_is_byte_identical_to_pre_phase5_payload(self):
        mock_client = _make_mock_client(_chat_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(prompt_caching=False)
            await backend.start_session("sys prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["messages"][0] == {"role": "system", "content": "sys prompt"}


class TestPromptCacheControlMessages:
    """Phase 5's speculative cache_control breakpoint on the hand-built /messages
    protocol path, plus its dedicated _CacheRejected degrade-on-4xx wrapper around
    _call_messages_api (this protocol does not go through the shared _call_api at
    all, so it needed its own copy of the catch-and-retry-clean logic)."""

    async def test_permanent_4xx_with_cache_fields_degrades_and_retries_clean(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=400, json=MagicMock(return_value=_messages_error_body()), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_messages_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert backend._prompt_caching is False
        assert mock_client.post.call_count == 2
        retried_payload = mock_client.post.call_args.kwargs["json"]
        assert retried_payload["system"] == "sys"

    async def test_permanent_4xx_persists_after_clean_retry_also_fails_as_plain_unavailable(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=400, json=MagicMock(return_value=_messages_error_body()), text="x"),
                MagicMock(status_code=400, json=MagicMock(return_value=_messages_error_body("bad model")), text="z"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend.start_session("sys", "hi")
        assert type(exc_info.value) is AgentBackendUnavailable
        assert backend._prompt_caching is False

    async def test_permanent_4xx_without_prompt_caching_does_not_retry(self):
        mock_client = _make_mock_client(_messages_error_body("bad model"), status_code=400)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max", prompt_caching=False)
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.call_count == 1

    async def test_cache_tokens_logged_when_present(self, caplog):
        body = _messages_body(FINAL_RAW)
        body["usage"] = {"cache_read_input_tokens": 111, "cache_creation_input_tokens": 222}
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level("INFO"):
                backend = OpenCodeGoBackend(model="qwen3.8-max")
                await backend.start_session("sys", "hi")
        assert "cache_read_input_tokens=111" in caplog.text
        assert "cache_creation_input_tokens=222" in caplog.text

    async def test_missing_usage_does_not_raise(self):
        mock_client = _make_mock_client(_messages_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeGoBackend(model="qwen3.8-max")
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
