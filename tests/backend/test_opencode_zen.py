"""Tests for jsa/agents/opencode_zen.py: OpenCodeZenBackend and OpenCodeZenSessionHandle.

`httpx.AsyncClient` is imported at module scope in opencode_zen.py, so patching
`httpx.AsyncClient` globally (matching how test_anthropic_api.py patches
`anthropic.AsyncAnthropic`) affects the shared module object opencode_zen.py reads from.

All tests are async; asyncio_mode = "auto" is set in pyproject.toml.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentChunk,
    AgentLimitReached,
    AgentTimeout,
    HistoryTurn,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.opencode_zen import (
    OpenCodeZenBackend,
    OpenCodeZenSessionHandle,
    _ToolsRejected,
)
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import to_openai_tools, tools_for
from jsa.db.models import Stage
from jsa.schema.turn_models import json_schema_for


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """_call_api's retry backoff uses real asyncio.sleep — replace with a no-op so
    tests exercising the retryable/transient path don't actually wait ~4s each."""
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents.opencode_zen.asyncio.sleep", _instant_sleep)

FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"
NEED_INPUT_RAW = "<<<NEED_INPUT>>>\nWhat is your target industry?\n<<<END>>>"
NO_SENTINEL_RAW = "Shall I proceed with this strategy, or would you like to adjust anything?"
UNTERMINATED_RAW = "<<<FINAL>>>\nhalf-written, never closed"


def _make_mock_client(json_body: dict, status_code: int = 200) -> MagicMock:
    """Return an async-compatible mock httpx.AsyncClient whose post() returns json_body."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json = MagicMock(return_value=json_body)
    mock_response.text = str(json_body)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


def _completion_body(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _make_mock_client_response_sequence(responses: list[tuple[int, dict | None, str | None]]) -> MagicMock:
    """Return a mock httpx.AsyncClient whose post() yields one (status, json_body,
    raw_text) response per call, in order. json_body=None simulates a non-JSON body
    (response.json() raises ValueError) — raw_text is used instead."""
    mocks = []
    for status_code, json_body, raw_text in responses:
        resp = MagicMock()
        resp.status_code = status_code
        if json_body is None:
            resp.json = MagicMock(side_effect=ValueError("not JSON"))
            resp.text = raw_text or ""
        else:
            resp.json = MagicMock(return_value=json_body)
            resp.text = raw_text if raw_text is not None else str(json_body)
        mocks.append(resp)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=mocks)
    mock_client.aclose = AsyncMock()
    return mock_client


def _make_mock_client_sequence(texts: list[str]) -> MagicMock:
    """Return a mock httpx.AsyncClient whose post() yields one body per call, in order."""
    responses = []
    for text in texts:
        resp = MagicMock()
        resp.status_code = 200
        body = _completion_body(text)
        resp.json = MagicMock(return_value=body)
        resp.text = str(body)
        responses.append(resp)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=responses)
    mock_client.aclose = AsyncMock()
    return mock_client


# ---------------------------------------------------------------------------
# 1 — start_session: happy path with FINAL reply
# ---------------------------------------------------------------------------

class TestStartSessionFinal:
    async def test_kind_is_final(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "initial user message")
        assert reply.kind == "final"

    async def test_content_is_inner_text(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "initial user message")
        assert reply.content == "Adjusted CV content here."

    async def test_handle_external_id_is_none(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert handle.external_id is None

    async def test_handle_messages_contains_user_and_assistant_turns(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert len(handle.messages) == 2
        assert handle.messages[0] == {"role": "user", "content": "initial user message"}
        assert handle.messages[1] == {"role": "assistant", "content": FINAL_RAW}

    async def test_handle_is_opencode_zen_session_handle(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "initial user message")
        assert isinstance(handle, OpenCodeZenSessionHandle)

    async def test_handle_system_prompt_stored(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("my system prompt", "msg")
        assert handle.system_prompt == "my system prompt"


# ---------------------------------------------------------------------------
# 2 — start_session: NEED_INPUT reply
# ---------------------------------------------------------------------------

class TestStartSessionNeedInput:
    async def test_kind_is_needs_input(self):
        mock_client = _make_mock_client(_completion_body(NEED_INPUT_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "hello")
        assert reply.kind == "needs_input"

    async def test_question_is_populated(self):
        mock_client = _make_mock_client(_completion_body(NEED_INPUT_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "hello")
        assert reply.question == "What is your target industry?"


# ---------------------------------------------------------------------------
# 3 — restore_session: no API call, history reconstructed
# ---------------------------------------------------------------------------

class TestRestoreSession:
    async def test_no_api_call_is_made(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            history = [
                HistoryTurn(role="user", content="q"),
                HistoryTurn(role="assistant", content="a"),
            ]
            await backend.restore_session("sys", history, external_id=None)
        mock_client.post.assert_not_called()

    async def test_messages_reconstructed_from_history(self):
        backend = OpenCodeZenBackend()
        history = [
            HistoryTurn(role="user", content="q"),
            HistoryTurn(role="assistant", content="a"),
        ]
        handle = await backend.restore_session("sys", history, external_id=None)
        assert handle.messages == [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]

    async def test_external_id_ignored_always_none(self):
        """REST API is stateless; external_id is always None even if one was passed in."""
        backend = OpenCodeZenBackend()
        handle = await backend.restore_session("sys", [], external_id="some-token")
        assert handle.external_id is None

    async def test_handle_is_opencode_zen_session_handle(self):
        backend = OpenCodeZenBackend()
        handle = await backend.restore_session("sys", [], external_id=None)
        assert isinstance(handle, OpenCodeZenSessionHandle)


# ---------------------------------------------------------------------------
# 4 — send_message: appends turns and calls API correctly
# ---------------------------------------------------------------------------

class TestSendMessage:
    async def test_handle_has_four_messages_after_send(self):
        handle = OpenCodeZenSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": FINAL_RAW},
            ],
        )
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.send_message(handle, "follow-up question")
        assert len(handle.messages) == 4
        assert handle.messages[2] == {"role": "user", "content": "follow-up question"}
        assert handle.messages[3] == {"role": "assistant", "content": FINAL_RAW}

    async def test_api_called_with_system_message_first(self):
        handle = OpenCodeZenSessionHandle(
            id="test-id", external_id=None, system_prompt="my system prompt", messages=[],
        )
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.send_message(handle, "user text")
        call_kwargs = mock_client.post.call_args.kwargs
        sent_messages = call_kwargs["json"]["messages"]
        assert sent_messages[0] == {"role": "system", "content": "my system prompt"}
        assert sent_messages[-1] == {"role": "user", "content": "user text"}

    async def test_api_called_with_model(self):
        handle = OpenCodeZenSessionHandle(id="test-id", external_id=None, system_prompt="sys", messages=[])
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend(model="some-other-model")
            await backend.send_message(handle, "user text")
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "some-other-model"

    async def test_api_called_with_bearer_auth_header(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_API_KEY", "test-key-123")
        handle = OpenCodeZenSessionHandle(id="test-id", external_id=None, system_prompt="sys", messages=[])
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.send_message(handle, "user text")
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["headers"]["Authorization"] == "Bearer test-key-123"

    async def test_api_called_against_correct_endpoint(self):
        handle = OpenCodeZenSessionHandle(id="test-id", external_id=None, system_prompt="sys", messages=[])
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.send_message(handle, "user text")
        call_args = mock_client.post.call_args.args
        assert call_args[0] == "https://opencode.ai/zen/v1/chat/completions"


# ---------------------------------------------------------------------------
# 5 — end_session: clears messages
# ---------------------------------------------------------------------------

class TestEndSession:
    async def test_messages_cleared(self):
        handle = OpenCodeZenSessionHandle(
            id="test-id",
            external_id=None,
            system_prompt="sys",
            messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": FINAL_RAW}],
        )
        backend = OpenCodeZenBackend()
        await backend.end_session(handle)
        assert handle.messages == []


# ---------------------------------------------------------------------------
# 6 — _call_api: timeout raises AgentTimeout
# ---------------------------------------------------------------------------

class TestCallApiTimeout:
    async def test_timeout_raises_agent_timeout(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend(timeout=0.01)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "initial message")

    async def test_agent_timeout_message_mentions_timeout_value(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend(timeout=0.01)
            with pytest.raises(AgentTimeout, match="0.01"):
                await backend.start_session("sys", "initial message")


# ---------------------------------------------------------------------------
# 7 — _call_api: error handling (rate limit / credits / generic)
# ---------------------------------------------------------------------------

class TestCallApiErrors:
    async def test_http_429_raises_agent_limit_reached(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg")

    async def test_200_with_credits_error_body_raises_agent_limit_reached(self):
        """OpenCode Zen can return HTTP 200 with an error payload in the body."""
        body = {"type": "error", "error": {"type": "CreditsError", "message": "Insufficient balance"}}
        mock_client = _make_mock_client(body, status_code=200)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg")

    async def test_model_error_raises_runtime_error(self):
        body = {"type": "error", "error": {"type": "ModelError", "message": "Model x is not supported"}}
        mock_client = _make_mock_client(body, status_code=401)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(RuntimeError, match="not supported"):
                await backend.start_session("sys", "msg")

    async def test_generic_5xx_raises_runtime_error(self):
        mock_client = _make_mock_client({"detail": "boom"}, status_code=500)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(RuntimeError):
                await backend.start_session("sys", "msg")

    async def test_non_json_5xx_body_raises_runtime_error(self):
        """A gateway-level 502/503 can return HTML/plain-text, not the API's JSON envelope."""
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.json = MagicMock(side_effect=ValueError("not JSON"))
        mock_response.text = "<html>Bad Gateway</html>"
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(RuntimeError, match="502"):
                await backend.start_session("sys", "msg")

    async def test_null_content_raises_runtime_error(self):
        """Reasoning models can return null content when truncated before a final answer."""
        body = {"choices": [{"message": {"role": "assistant", "content": None}}]}
        mock_client = _make_mock_client(body, status_code=200)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(RuntimeError, match="null"):
                await backend.start_session("sys", "msg")


# ---------------------------------------------------------------------------
# 7a — _call_api: transient overload/gateway retry vs. permanent-error classification
# (see CLAUDE.md "OpenCode Zen backend"). Transient signals (5xx, JSON
# "server_error"/"overloaded_error" envelope, null content) are retried on the
# SAME backend up to _MAX_ATTEMPTS before finally raising AgentBackendUnavailable
# (which engages BF-19 like AgentLimitReached/AgentTimeout). Permanent signals
# (a structured 4xx client/config error — bad model, bad request, non-JSON 4xx)
# raise AgentBackendUnavailable immediately, with no wasted retry.
# ---------------------------------------------------------------------------

class TestCallApiRetryClassification:
    async def test_transient_5xx_then_success_retries_same_backend(self):
        """A 500 on the first attempt, then a clean reply on the second: the
        retry must be transparent to the caller — succeeds, no exception."""
        mock_client = _make_mock_client_response_sequence([
            (500, {"detail": "temporarily overloaded"}, None),
            (200, _completion_body(FINAL_RAW), None),
        ])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg")
        assert reply.kind == "final"
        assert mock_client.post.call_count == 2

    async def test_server_error_envelope_is_retried_then_succeeds(self):
        """HTTP 200 with a JSON {"error": {"type": "server_error"}} envelope
        (the live-observed transient-upstream-502 shape from CLAUDE.md) is
        retryable even though the status code itself is 200."""
        body = {"type": "error", "error": {"type": "server_error", "message": "upstream overloaded"}}
        mock_client = _make_mock_client_response_sequence([
            (200, body, None),
            (200, _completion_body(FINAL_RAW), None),
        ])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg")
        assert reply.kind == "final"
        assert mock_client.post.call_count == 2

    async def test_transient_error_exhausts_retries_raises_backend_unavailable(self):
        """All _MAX_ATTEMPTS (3) attempts fail with a transient 500 → switches to
        AgentBackendUnavailable (not a plain RuntimeError), engaging BF-19."""
        mock_client = _make_mock_client_response_sequence([
            (500, {"detail": "boom"}, None),
            (500, {"detail": "boom"}, None),
            (500, {"detail": "boom"}, None),
        ])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="3 attempts"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 3

    async def test_null_content_exhausts_retries_raises_backend_unavailable(self):
        body = {"choices": [{"message": {"role": "assistant", "content": None}}]}
        mock_client = _make_mock_client_response_sequence([(200, body, None)] * 3)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="null"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 3

    async def test_non_json_5xx_exhausts_retries_raises_backend_unavailable(self):
        mock_client = _make_mock_client_response_sequence(
            [(502, None, "<html>Bad Gateway</html>")] * 3
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="502"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 3

    async def test_bad_model_error_switches_backend_immediately_no_retry(self):
        """A structured 4xx config/client error (bad model name, malformed
        request) is NOT retried — it can't be fixed by calling the same backend
        again — but DOES raise AgentBackendUnavailable so BF-19 switches."""
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "Model x is not supported"}}
        mock_client = _make_mock_client({}, status_code=400)
        mock_client.post.return_value.json = MagicMock(return_value=body)
        mock_client.post.return_value.text = str(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="not supported"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 1

    async def test_non_json_4xx_switches_backend_immediately_no_retry(self):
        mock_client = _make_mock_client_response_sequence([(404, None, "Not Found")])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="404"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 1

    async def test_rate_limit_body_switches_immediately_no_retry(self):
        """Existing AgentLimitReached path is unretried and unchanged by this work."""
        body = {"type": "error", "error": {"type": "RateLimitError", "message": "rate limited"}}
        mock_client = _make_mock_client(body, status_code=200)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 1

    async def test_http_429_switches_immediately_no_retry(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# 7b — nudge-and-retry on a missing sentinel block (mirrors ClaudeCliBackend's
# _parse_with_nudge; regression coverage for the free/flaky-model non-compliance
# bug where a reply like NO_SENTINEL_RAW was previously an uncaught ProtocolError
# that hard-failed the job on the very first turn).
# ---------------------------------------------------------------------------

class TestNudgeOnMissingSentinel:
    async def test_happy_path_no_nudge_needed(self):
        mock_client = _make_mock_client_sequence([FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg")
        assert reply.kind == "final"
        assert mock_client.post.call_count == 1

    async def test_missing_sentinel_then_nudge_succeeds(self):
        mock_client = _make_mock_client_sequence([NO_SENTINEL_RAW, NEED_INPUT_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 2
        assert reply.kind == "needs_input"
        assert reply.question == "What is your target industry?"
        # The nudge exchange itself is not persisted — only the corrected reply is,
        # mirroring ClaudeCliBackend's contract (stages.py only ever records reply.raw).
        assert handle.messages[-1] == {"role": "assistant", "content": NEED_INPUT_RAW}
        assert len(handle.messages) == 2

    async def test_nudge_request_includes_correction_text(self):
        mock_client = _make_mock_client_sequence([NO_SENTINEL_RAW, NEED_INPUT_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg")
        second_call_messages = mock_client.post.call_args_list[1].kwargs["json"]["messages"]
        assert second_call_messages[-2] == {"role": "assistant", "content": NO_SENTINEL_RAW}
        assert "sentinel block" in second_call_messages[-1]["content"]

    async def test_missing_sentinel_twice_propagates_protocol_error(self):
        mock_client = _make_mock_client_sequence([NO_SENTINEL_RAW, NO_SENTINEL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(ProtocolError, match="no sentinel block"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 2

    async def test_unterminated_block_is_not_nudged(self):
        """A different ProtocolError (unterminated block) is not the nudge's target — propagate immediately."""
        mock_client = _make_mock_client_sequence([UNTERMINATED_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(ProtocolError, match="unterminated block"):
                await backend.start_session("sys", "msg")
        assert mock_client.post.call_count == 1

    async def test_send_message_nudges_on_missing_sentinel(self):
        handle = OpenCodeZenSessionHandle(
            id="test-id", external_id=None, system_prompt="sys",
            messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": FINAL_RAW}],
        )
        mock_client = _make_mock_client_sequence([NO_SENTINEL_RAW, FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            reply = await backend.send_message(handle, "follow-up")
        assert mock_client.post.call_count == 2
        assert reply.kind == "final"
        assert handle.messages[-1] == {"role": "assistant", "content": FINAL_RAW}


# ---------------------------------------------------------------------------
# 8 — Registry: backend_for("opencode-zen") returns OpenCodeZenBackend
# ---------------------------------------------------------------------------

class TestRegistryOpenCodeZenBackend:
    def test_backend_for_opencode_zen_returns_correct_class(self):
        from jsa.agents.registry import backend_for
        b = backend_for("opencode-zen")
        assert isinstance(b, OpenCodeZenBackend)

    def test_backend_for_opencode_zen_name_attribute(self):
        from jsa.agents.registry import backend_for
        b = backend_for("opencode-zen")
        assert b.name == "opencode-zen"

    def test_default_model_is_nemotron(self):
        backend = OpenCodeZenBackend()
        assert backend._model == "nemotron-3-ultra-free"


# ---------------------------------------------------------------------------
# 9 — Config: new fields have correct defaults
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_opencode_zen_model_default(self, monkeypatch):
        monkeypatch.delenv("JSA_OPENCODE_ZEN_MODEL", raising=False)
        from jsa.config import Settings
        s = Settings()
        assert s.opencode_zen_model == "nemotron-3-ultra-free"

    def test_opencode_zen_timeout_default(self, monkeypatch):
        monkeypatch.delenv("JSA_OPENCODE_ZEN_TIMEOUT", raising=False)
        from jsa.config import Settings
        s = Settings()
        # 300s, not 180s: the five HTTP API backends were bumped in Phase 1 of the
        # model-fallback-ladder plan. A busy-but-alive model on a throttled provider
        # account routinely needs longer than 180s for one reply, and BF-19 reads that
        # timeout as "backend down" and burns a whole chain hop. See
        # tests/backend/test_orchestrator_throttling.py::TestSettingsDefaults for the
        # full set (and for the pin that anthropic_timeout/agent_timeout are NOT bumped).
        assert s.opencode_zen_timeout == 300.0

    def test_opencode_zen_model_overridable_via_env(self, monkeypatch):
        monkeypatch.setenv("JSA_OPENCODE_ZEN_MODEL", "gpt-5")
        from jsa.config import Settings
        s = Settings()
        assert s.opencode_zen_model == "gpt-5"


# ---------------------------------------------------------------------------
# 10 — make_backend_factory wiring
# ---------------------------------------------------------------------------

class TestMakeBackendFactory:
    def test_factory_returns_opencode_zen_backend(self):
        from jsa.config import Settings
        from jsa.server import make_backend_factory
        settings = Settings()
        factory = make_backend_factory(settings)
        agent = factory("opencode-zen")
        assert isinstance(agent, OpenCodeZenBackend)

    def test_factory_uses_opencode_zen_model_not_shared_model(self):
        from jsa.config import Settings
        from jsa.server import make_backend_factory
        settings = Settings(model="claude-haiku-4-5", opencode_zen_model="gemini-3.5-flash")
        factory = make_backend_factory(settings)
        agent = factory("opencode-zen")
        assert agent._model == "gemini-3.5-flash"

    def test_factory_model_override_applies_to_opencode_zen(self):
        from jsa.config import Settings
        from jsa.server import make_backend_factory
        settings = Settings()
        factory = make_backend_factory(settings, model_override="gpt-5")
        agent = factory("opencode-zen")
        assert agent._model == "gpt-5"


# ---------------------------------------------------------------------------
# Bonus — TypeError for wrong handle type
# ---------------------------------------------------------------------------

class TestTypeErrorOnWrongHandle:
    async def test_send_message_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle
        wrong_handle = SessionHandle(id="bad", external_id=None)
        backend = OpenCodeZenBackend()
        with pytest.raises(TypeError, match="OpenCodeZenSessionHandle"):
            await backend.send_message(wrong_handle, "some text")

    async def test_end_session_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle
        wrong_handle = SessionHandle(id="bad", external_id=None)
        backend = OpenCodeZenBackend()
        with pytest.raises(TypeError, match="OpenCodeZenSessionHandle"):
            await backend.end_session(wrong_handle)


# ---------------------------------------------------------------------------
# Structured output (Phase 4) — capability flag, request shape, replies,
# per-session downgrade, and retry/downgrade independence.
# ---------------------------------------------------------------------------

CV_SCHEMA = json_schema_for(Stage.cv_adjust)
FIT_SCHEMA = json_schema_for(Stage.fit_assessment)


def _cv_payload() -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [{"name": "Summary", "text": "Senior engineer."}],
    }


def _structured_final_body(payload: dict) -> dict:
    return _completion_body(json.dumps({"kind": "final", "question": None, "payload": payload}))


def _structured_question_body(question: str) -> dict:
    return _completion_body(json.dumps({"kind": "question", "question": question, "payload": None}))


class TestSupportsStructuredOutput:
    def test_flag_is_true(self):
        assert OpenCodeZenBackend.supports_structured_output is True


class TestStructuredRequestShape:
    async def test_response_format_sent_when_schema_given(self):
        mock_client = _make_mock_client(_structured_final_body(_cv_payload()))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        call_kwargs = mock_client.post.call_args.kwargs
        rf = call_kwargs["json"]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["schema"] == CV_SCHEMA
        assert rf["json_schema"]["strict"] is False

    async def test_no_response_format_when_schema_none(self):
        """None-schema → byte-identical to pre-Phase-4 payload shape."""
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg")
        call_kwargs = mock_client.post.call_args.kwargs
        assert "response_format" not in call_kwargs["json"]


class TestStructuredFinalReply:
    async def test_kind_final_and_content_is_payload(self):
        payload = _cv_payload()
        mock_client = _make_mock_client(_structured_final_body(payload))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "final"
        assert json.loads(reply.content) == payload

    async def test_raw_is_canonical_json_not_provider_envelope(self):
        payload = _cv_payload()
        mock_client = _make_mock_client(_structured_final_body(payload))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.raw.startswith("{")
        assert json.loads(reply.raw) == {"kind": "final", "question": None, "payload": payload}

    async def test_handle_structured_schema_and_enabled_stamped(self):
        mock_client = _make_mock_client(_structured_final_body(_cv_payload()))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert handle.structured_schema == CV_SCHEMA
        assert handle.structured_enabled is True


class TestStructuredQuestionReply:
    async def test_kind_needs_input_and_question_populated(self):
        mock_client = _make_mock_client(_structured_question_body("Which dates?"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "needs_input"
        assert reply.question == "Which dates?"
        assert reply.content == "Which dates?"


class TestStructuredFitVerdict:
    async def test_fit_content_matches_sentinel_parser_shape(self):
        body = _completion_body(json.dumps({"verdict": "UNFIT", "reason": "no relevant experience"}))
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            _, reply = await backend.start_session("sys", "msg", structured_schema=FIT_SCHEMA)
        assert reply.kind == "final"
        assert reply.content == "UNFIT\nno relevant experience"

    async def test_malformed_fit_reply_still_nudges(self):
        """Pinned per advisor: this method has no per-stage knowledge of the
        fit_assessment stage's one-shot policy (_run_fit_assessment catches
        ProtocolError and parks at the 'unfit' modal with no resume path) — a
        malformed fit-schema reply downgrades-and-nudges exactly like a cv/cl
        turn does, costing a second API call before the ProtocolError-or-recovery
        outcome reaches the stage. Flagged for Phase 5's attention: whether the
        fit stage should special-case this upstream is an open question this
        phase deliberately does not resolve at the backend layer."""
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg", structured_schema=FIT_SCHEMA)
        assert reply.raw == FINAL_RAW
        assert handle.structured_enabled is False
        assert mock_client.post.call_count == 2


class TestSemanticFailureDoesNotDowngrade:
    async def test_structurally_valid_but_semantically_incomplete_payload_no_downgrade(self):
        """parse_structured_reply_for_schema only checks isinstance(payload, dict) —
        real CV-content validation happens downstream in _validate_final_content, not
        here — so a structurally-valid-but-semantically-incomplete payload (missing
        required CV fields) must parse successfully and must NOT trigger a downgrade
        or a second API call."""
        mock_client = _make_mock_client(_structured_final_body({}))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "final"
        assert handle.structured_enabled is True
        assert mock_client.post.call_count == 1


class TestRestoreSessionStructured:
    async def test_schema_given_enables_structured(self):
        backend = OpenCodeZenBackend()
        handle = await backend.restore_session(
            "sys", [], external_id=None, structured_schema=CV_SCHEMA
        )
        assert handle.structured_schema == CV_SCHEMA
        assert handle.structured_enabled is True

    async def test_schema_none_disables_structured(self):
        """A restored session is NOT unconditionally structured-enabled — it must be
        established from the caller's schema argument, or a restore whose history
        was replayed in sentinel form would claim structured mode it never earned."""
        backend = OpenCodeZenBackend()
        handle = await backend.restore_session("sys", [], external_id=None)
        assert handle.structured_schema is None
        assert handle.structured_enabled is False


class TestDowngradeOnUnparseableStructuredReply:
    async def test_non_json_reply_downgrades_and_nudge_recovers(self):
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "final"
        assert reply.raw == FINAL_RAW
        assert handle.structured_enabled is False
        assert mock_client.post.call_count == 2

    async def test_missing_kind_downgrades_and_nudge_recovers(self):
        malformed = json.dumps({"question": None, "payload": _cv_payload()})  # no "kind"
        mock_client = _make_mock_client_sequence([malformed, FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "final"
        assert handle.structured_enabled is False

    async def test_downgrade_nudge_uses_mode_conditional_wording(self):
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        nudge_call_kwargs = mock_client.post.call_args_list[1].kwargs
        nudge_user_msg = nudge_call_kwargs["json"]["messages"][-1]["content"]
        assert "no longer applies" in nudge_user_msg

    async def test_downgrade_costs_exactly_one_api_call_before_the_nudge(self):
        """The malformed structured reply must not burn the in-backend
        _MAX_ATTEMPTS retry budget — downgrade is a parse-layer decision made
        strictly after _call_api has already returned successfully, so the
        malformed attempt plus the recovery nudge is exactly 2 calls, never 3+."""
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert mock_client.post.call_count == 2

    async def test_nudge_call_omits_response_format(self):
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        nudge_call_kwargs = mock_client.post.call_args_list[1].kwargs
        assert "response_format" not in nudge_call_kwargs["json"]

    async def test_downgrade_persists_to_subsequent_send_message_calls(self):
        """The downgrade flag sticks for the rest of the session: a later
        send_message call must never re-attempt response_format, even though the
        caller (stages.py) has no visibility into this backend-internal state and
        keeps passing the stage's schema on every call."""
        mock_client = _make_mock_client_sequence(["not json at all", FINAL_RAW, FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
            await backend.send_message(handle, "follow-up", structured_schema=CV_SCHEMA)
        third_call_kwargs = mock_client.post.call_args_list[2].kwargs
        assert "response_format" not in third_call_kwargs["json"]
        assert handle.structured_enabled is False

    async def test_second_failure_after_downgrade_propagates_protocol_error(self):
        """If the nudge's own reply also lacks a sentinel block, the ordinary
        second-failure propagation applies unchanged."""
        mock_client = _make_mock_client_sequence(["not json at all", "still not json"])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(ProtocolError):
                await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)

    async def test_downgrade_occurring_mid_conversation_on_send_message(self):
        """The more likely real-world shape: the session STARTS structured and its
        first turn parses fine; the downgrade only happens on a later send_message
        turn. This is the path where handle.structured_enabled's mutation inside
        send_message (as opposed to the constructor call in start_session) actually
        matters, and where a leaked nudge turn would first become visible."""
        mock_client = _make_mock_client_sequence(
            [
                json.dumps({"kind": "final", "question": None, "payload": _cv_payload()}),
                "not json at all",  # second turn: malformed, triggers downgrade
                FINAL_RAW,  # nudge recovery
                FINAL_RAW,  # third turn: sentinel mode from here on
            ]
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, first_reply = await backend.start_session(
                "sys", "msg", structured_schema=CV_SCHEMA
            )
            assert handle.structured_enabled is True

            second_reply = await backend.send_message(handle, "turn 2", structured_schema=CV_SCHEMA)
            assert handle.structured_enabled is False
            assert second_reply.raw == FINAL_RAW

            third_reply = await backend.send_message(handle, "turn 3", structured_schema=CV_SCHEMA)
        assert third_reply.raw == FINAL_RAW
        third_call_kwargs = mock_client.post.call_args_list[3].kwargs
        assert "response_format" not in third_call_kwargs["json"]
        # No leaked nudge turns: 2 turns per send_message (user + assistant), none
        # from the intermediate downgrade-nudge exchange.
        assert len(handle.messages) == 6
        assert [m["role"] for m in handle.messages] == [
            "user", "assistant", "user", "assistant", "user", "assistant",
        ]


class TestRetryDowngradeIndependence:
    async def test_transient_error_then_successful_structured_reply_stays_enabled(self):
        """A 5xx followed by a valid structured reply must leave structured_enabled
        True — the in-backend transient-retry loop and the parse-layer downgrade
        must never interfere with each other."""
        responses = [
            (502, {"detail": "boom"}, None),
            (200, _structured_final_body(_cv_payload()), None),
        ]
        mock_client = _make_mock_client_response_sequence(responses)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, reply = await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        assert reply.kind == "final"
        assert handle.structured_enabled is True
        assert mock_client.post.call_count == 2  # one in-loop retry, no downgrade nudge

    async def test_retry_attempts_keep_sending_response_format(self):
        """Each retry attempt inside _call_api retries the SAME request — the
        schema must not silently drop out on a retried attempt."""
        responses = [
            (502, {"detail": "boom"}, None),
            (200, _structured_final_body(_cv_payload()), None),
        ]
        mock_client = _make_mock_client_response_sequence(responses)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend.start_session("sys", "msg", structured_schema=CV_SCHEMA)
        for call in mock_client.post.call_args_list:
            assert "response_format" in call.kwargs["json"]


# ---------------------------------------------------------------------------
# Streaming — this backend keeps its own independent copy of the SSE-consuming
# machinery (see the module docstring), so it needs its own coverage rather
# than relying on test_streaming_openai_compat.py's MistralBackend-based tests.
# ---------------------------------------------------------------------------

def _sse_lines(events: list[dict]) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]


def _make_mock_stream_client(lines: list[str]) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200

    async def _aiter_lines():
        for line in lines:
            yield line

    mock_response.aiter_lines = _aiter_lines
    mock_response.aread = AsyncMock(return_value=b"")

    class _StreamCtx:
        async def __aenter__(self):
            return mock_response

        async def __aexit__(self, *a):
            return False

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=_StreamCtx())
    mock_client.aclose = AsyncMock()
    return mock_client


class TestStreamingBehavior:
    async def test_sentinel_mode_streams_content_and_reasoning_unfiltered(self):
        lines = _sse_lines(
            [
                {"choices": [{"delta": {"reasoning_content": "weighing options..."}}]},
                {"choices": [{"delta": {"content": FINAL_RAW}}]},
            ]
        )
        mock_client = _make_mock_stream_client(lines)
        received: list[AgentChunk] = []

        async def on_chunk(chunk: AgentChunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            raw = await backend._call_api("sys", [{"role": "user", "content": "hi"}], None, on_chunk)

        assert raw == FINAL_RAW
        assert received == [
            AgentChunk(kind="reasoning", text="weighing options..."),
            AgentChunk(kind="content", text=FINAL_RAW),
        ]

    async def test_structured_mode_streams_reasoning_but_suppresses_content(self):
        """Structured mode DOES attempt SSE when on_chunk is supplied — some routed
        models expose a genuine reasoning_content delta even under a forced JSON
        schema — but the content delta is raw partial JSON there, so on_chunk must
        only ever receive reasoning chunks. The full JSON is still returned as the
        raw reply regardless of what's forwarded to on_chunk."""
        raw_json = json.dumps({"kind": "final", "question": None, "payload": _cv_payload()})
        lines = _sse_lines(
            [
                {"choices": [{"delta": {"reasoning_content": "checking the JD..."}}]},
                {"choices": [{"delta": {"content": raw_json}}]},
            ]
        )
        mock_client = _make_mock_stream_client(lines)
        received: list[AgentChunk] = []

        async def on_chunk(chunk: AgentChunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            raw = await backend._call_api(
                "sys", [{"role": "user", "content": "hi"}], CV_SCHEMA, on_chunk
            )

        assert raw == raw_json
        assert received == [AgentChunk(kind="reasoning", text="checking the JD...")]
        mock_client.stream.assert_called_once()

    async def test_structured_mode_no_on_chunk_stays_non_streaming(self):
        mock_client = _make_mock_client(_structured_final_body(_cv_payload()))
        mock_client.stream = MagicMock(side_effect=AssertionError("must not stream"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            await backend._call_api("sys", [{"role": "user", "content": "hi"}], CV_SCHEMA, None)
        mock_client.post.assert_awaited_once()


# ---------------------------------------------------------------------------
# Native tool calling (revision-tool-use plan, Phase 3 / E3). This backend keeps
# its own independent copy of the /chat/completions machinery (see the module
# docstring and CLAUDE.md), so it needs its own tool coverage rather than
# relying on test_openai_compat.py's shared-base tests.
# ---------------------------------------------------------------------------

CV_TOOLS = tools_for(Stage.revising_cv)


def _wire_call(call_id: str, name: str, arguments: str) -> dict:
    """One OpenAI-shape `message.tool_calls` entry — `arguments` is a JSON STRING
    on this wire shape, which is exactly what the backend has to decode."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _tool_call_body(wire_calls: list[dict]) -> dict:
    """A completion whose message carries tool calls and `content: null` — the shape
    every provider returns for a forced tool call, and the one the pre-tools
    null-content transient check would have misclassified."""
    return {
        "choices": [
            {"message": {"role": "assistant", "content": None, "tool_calls": wire_calls}}
        ]
    }


class TestSupportsNativeTools:
    def test_flag_is_true(self):
        assert OpenCodeZenBackend.supports_native_tools is True


class TestNativeToolRequestShape:
    async def test_tools_and_tool_choice_required_are_sent(self):
        mock_client = _make_mock_client(
            _tool_call_body([_wire_call("call_abc", "get_cv", "{}")])
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            await backend.send_message(handle, "tighten the summary")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["tools"] == to_openai_tools(CV_TOOLS)
        assert payload["tool_choice"] == "required"

    async def test_no_response_format_when_tools_active(self):
        """Tool mode and structured mode are mutually exclusive per request — the
        terminal tool's arguments ARE the structured output."""
        mock_client = _make_mock_client(
            _tool_call_body([_wire_call("call_abc", "get_cv", "{}")])
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            await backend.send_message(handle, "tighten the summary")
        assert "response_format" not in mock_client.post.call_args.kwargs["json"]

    async def test_no_tools_key_when_tools_none(self):
        """A non-tool session's payload is byte-identical to its pre-tools shape —
        asserted on EVERY POST of the session, not just the last one, so a regression
        leaking tools into a mid-session call can't hide behind the final payload."""
        mock_client = _make_mock_client_sequence([FINAL_RAW, FINAL_RAW])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle, _ = await backend.start_session("sys", "msg")
            await backend.send_message(handle, "follow-up")
        assert mock_client.post.call_count == 2
        for call in mock_client.post.call_args_list:
            assert "tools" not in call.kwargs["json"]
            assert "tool_choice" not in call.kwargs["json"]

    async def test_tools_with_structured_schema_is_rejected(self):
        backend = OpenCodeZenBackend()
        with pytest.raises(ValueError, match="mutually exclusive"):
            await backend.restore_session(
                "sys", [], None, structured_schema=CV_SCHEMA, tools=CV_TOOLS
            )


class TestNativeToolReplyExtraction:
    async def test_null_content_with_tool_calls_is_success_not_transient(self):
        """THE trap: a forced tool call comes back with `content: null`, which the
        pre-tools null-content check treats as a retryable transient failure. It must
        be extracted BEFORE that check — one HTTP attempt, no retry."""
        mock_client = _make_mock_client(
            _tool_call_body([_wire_call("call_abc", "get_cv", "{}")])
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            reply = await backend.send_message(handle, "revise")
        assert reply.kind == "tool_calls"
        assert mock_client.post.call_count == 1

    async def test_multiple_calls_returned_in_order_with_provider_ids(self):
        wire = [
            _wire_call("call_zen_1", "get_cv", "{}"),
            _wire_call(
                "call_zen_2",
                "edit_entry_bullets",
                json.dumps({"entry_id": "e1", "bullets": ["a", "b"]}),
            ),
            _wire_call("call_zen_3", "finalize", json.dumps({"change_log": "tightened"})),
        ]
        mock_client = _make_mock_client(_tool_call_body(wire))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            reply = await backend.send_message(handle, "revise")
        assert [c.name for c in reply.tool_calls] == [
            "get_cv", "edit_entry_bullets", "finalize",
        ]
        assert [c.id for c in reply.tool_calls] == [
            "call_zen_1", "call_zen_2", "call_zen_3",
        ]
        # arguments is always a plain dict — never a string awaiting a second parse
        assert reply.tool_calls[1].arguments == {"entry_id": "e1", "bullets": ["a", "b"]}
        assert reply.tool_calls[0].arguments == {}

    async def test_malformed_arguments_json_raises_protocol_error(self):
        mock_client = _make_mock_client(
            _tool_call_body([_wire_call("call_abc", "get_cv", "{not json")])
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(ProtocolError, match="not valid JSON"):
                await backend.send_message(handle, "revise")

    async def test_tools_sent_but_null_content_and_no_calls_still_retries(self):
        """The mirror of the trap above: tools were sent but the model returned
        neither calls nor content. That is still the genuine transient null-content
        failure the pre-tools check exists for — it must keep retrying."""
        body = {"choices": [{"message": {"role": "assistant", "content": None}}]}
        mock_client = _make_mock_client_response_sequence([(200, body, None)] * 3)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(AgentBackendUnavailable, match="null"):
                await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 3

    async def test_empty_tool_calls_list_with_null_content_still_retries(self):
        body = {
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": []}}]
        }
        mock_client = _make_mock_client_response_sequence([(200, body, None)] * 3)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(AgentBackendUnavailable, match="null"):
                await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 3


class TestSendToolResults:
    async def test_one_tool_message_per_result_with_matching_ids(self):
        first = _tool_call_body([
            _wire_call("call_zen_1", "get_cv", "{}"),
            _wire_call("call_zen_2", "remove_entry", json.dumps({"entry_id": "e9"})),
        ])
        second = _tool_call_body([
            _wire_call("call_zen_3", "finalize", json.dumps({"change_log": "done"}))
        ])
        mock_client = _make_mock_client_response_sequence([(200, first, None), (200, second, None)])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            await backend.send_message(handle, "revise")
            reply = await backend.send_tool_results(
                handle,
                [
                    ToolResult(call_id="call_zen_1", name="get_cv", ok=True, content={"sections": []}),
                    ToolResult(
                        call_id="call_zen_2", name="remove_entry", ok=False,
                        content={"ok": False, "error": {"code": "bad_argument"}},
                    ),
                ],
            )
        sent = mock_client.post.call_args_list[1].kwargs["json"]["messages"]
        tool_rows = [m for m in sent if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_rows] == ["call_zen_1", "call_zen_2"]
        assert json.loads(tool_rows[0]["content"]) == {"sections": []}
        # The assistant turn carrying tool_calls precedes them on the wire
        assistant_rows = [m for m in sent if m["role"] == "assistant"]
        assert assistant_rows[-1]["tool_calls"][0]["id"] == "call_zen_1"
        assert sent.index(assistant_rows[-1]) < sent.index(tool_rows[0])
        assert reply.kind == "tool_calls"
        assert reply.tool_calls[0].name == "finalize"

    async def test_tools_still_attached_on_the_follow_up_post(self):
        first = _tool_call_body([_wire_call("call_zen_1", "get_cv", "{}")])
        second = _tool_call_body([
            _wire_call("call_zen_2", "finalize", json.dumps({"change_log": "done"}))
        ])
        mock_client = _make_mock_client_response_sequence([(200, first, None), (200, second, None)])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            await backend.send_message(handle, "revise")
            await backend.send_tool_results(
                handle, [ToolResult(call_id="call_zen_1", name="get_cv", ok=True, content={})]
            )
        follow_up = mock_client.post.call_args_list[1].kwargs["json"]
        assert follow_up["tools"] == to_openai_tools(CV_TOOLS)
        assert follow_up["tool_choice"] == "required"

    async def test_wrong_handle_type_raises_type_error(self):
        backend = OpenCodeZenBackend()
        with pytest.raises(TypeError):
            await backend.send_tool_results(object(), [])  # type: ignore[arg-type]

    async def test_session_without_tools_raises_rather_than_posting(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None)
            with pytest.raises(RuntimeError, match="not opened with tools"):
                await backend.send_tool_results(
                    handle, [ToolResult(call_id="c1", name="get_cv", ok=True, content={})]
                )
        assert mock_client.post.call_count == 0

    async def test_no_preceding_tool_call_turn_raises_rather_than_posting(self):
        """`role: "tool"` rows are only legal right after an assistant turn carrying
        the matching ids. tool_loop.py can't violate this, but a violation must fail
        loudly rather than as a proxy 400 misread as a tools rejection."""
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(RuntimeError, match="preceding assistant turn"):
                await backend.send_tool_results(
                    handle, [ToolResult(call_id="c1", name="get_cv", ok=True, content={})]
                )
        assert mock_client.post.call_count == 0


class TestToolsRejectedClassification:
    async def test_permanent_4xx_with_tools_raises_tools_rejected_unretried(self):
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "tools unsupported"}}
        mock_client = _make_mock_client(body, status_code=400)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(_ToolsRejected) as exc_info:
                await backend.send_message(handle, "revise")
        # It must reach tool_loop.py's rung ladder, NOT BF-19's backend advance
        assert isinstance(exc_info.value, ToolsUnsupported)
        assert not isinstance(exc_info.value, AgentBackendUnavailable)
        assert mock_client.post.call_count == 1

    async def test_non_json_4xx_with_tools_raises_tools_rejected_unretried(self):
        mock_client = _make_mock_client_response_sequence([(404, None, "Not Found")])
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(ToolsUnsupported):
                await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 1

    async def test_permanent_4xx_without_tools_still_raises_backend_unavailable(self):
        """BF-19 is unchanged for every non-tool turn — the gate is the payload
        actually sent, never the capability flag."""
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "Model x is not supported"}}
        mock_client = _make_mock_client(body, status_code=400)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            with pytest.raises(AgentBackendUnavailable, match="not supported") as exc_info:
                await backend.start_session("sys", "msg")
        assert not isinstance(exc_info.value, ToolsUnsupported)
        assert mock_client.post.call_count == 1

    async def test_transient_failure_with_tools_still_retries_three_times(self):
        """Branch 2 (transient) must be untouched by the tools path."""
        mock_client = _make_mock_client_response_sequence([(500, {"detail": "boom"}, None)] * 3)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(AgentBackendUnavailable, match="3 attempts"):
                await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 3

    async def test_retry_attempts_keep_sending_tools(self):
        responses = [
            (500, {"detail": "boom"}, None),
            (200, _tool_call_body([_wire_call("call_1", "get_cv", "{}")]), None),
        ]
        mock_client = _make_mock_client_response_sequence(responses)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            reply = await backend.send_message(handle, "revise")
        assert reply.kind == "tool_calls"
        for call in mock_client.post.call_args_list:
            assert "tools" in call.kwargs["json"]

    async def test_rate_limit_with_tools_still_raises_limit_reached(self):
        """Branch 1 (quota/rate) must be untouched too — a 429 is an account-scoped
        signal, not a statement about tool support."""
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            with pytest.raises(AgentLimitReached):
                await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 1


class TestToolModeDoesNotTouchStructuredOrNudge:
    async def test_restore_with_tools_leaves_structured_mode_off(self):
        backend = OpenCodeZenBackend()
        handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
        assert handle.tools == CV_TOOLS
        assert handle.structured_schema is None
        assert handle.structured_enabled is False

    async def test_prose_reply_in_tool_mode_never_nudges(self):
        """A tool-mode session was never given the sentinel contract, so a reply with
        no tool calls must NOT go through _parse_with_nudge — exactly one POST, and
        the structured downgrade flag is never touched."""
        mock_client = _make_mock_client(_completion_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenCodeZenBackend()
            handle = await backend.restore_session("sys", [], None, tools=CV_TOOLS)
            reply = await backend.send_message(handle, "revise")
        assert mock_client.post.call_count == 1
        assert reply.kind != "tool_calls"
        assert reply.raw == NO_SENTINEL_RAW
        assert handle.structured_enabled is False
