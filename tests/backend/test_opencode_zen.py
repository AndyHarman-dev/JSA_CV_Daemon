"""Tests for jsa/agents/opencode_zen.py: OpenCodeZenBackend and OpenCodeZenSessionHandle.

`httpx.AsyncClient` is imported at module scope in opencode_zen.py, so patching
`httpx.AsyncClient` globally (matching how test_anthropic_api.py patches
`anthropic.AsyncAnthropic`) affects the shared module object opencode_zen.py reads from.

All tests are async; asyncio_mode = "auto" is set in pyproject.toml.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached, AgentTimeout, HistoryTurn
from jsa.agents.opencode_zen import OpenCodeZenBackend, OpenCodeZenSessionHandle
from jsa.agents.protocol import ProtocolError


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
        assert s.opencode_zen_timeout == 180.0

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
