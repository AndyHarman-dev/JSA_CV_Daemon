"""Tests for jsa/agents/gemini_api.py's GeminiBackend.

Session/nudge/downgrade/retry orchestration is inherited unchanged from
OpenAICompatBackend (see tests/backend/test_openai_compat.py for that shared
machinery's own coverage via MistralBackend) — these tests focus on what
GeminiBackend actually overrides: the generateContent request/response shape,
its error envelope classification, the responseSchema inlining, and the
finishReason == "MAX_TOKENS" truncation check.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import OpenAICompatSessionHandle, _ToolsRejected
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentLimitReached,
    AgentTimeout,
    HistoryTurn,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.gemini_api import GeminiBackend
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import tools_for
from jsa.db.models import Stage
from jsa.schema.turn_models import json_schema_for


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"
NO_SENTINEL_RAW = "Shall I proceed, or would you like to adjust anything?"


def _make_mock_client(json_body: dict, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json = MagicMock(return_value=json_body)
    mock_response.text = str(json_body)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


def _gemini_body(text: str, finish_reason: str = "STOP") -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}
        ]
    }


def _cv_payload() -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [{"name": "Summary", "text": "Senior engineer."}],
    }


def _structured_final_body(payload: dict) -> dict:
    text = json.dumps({"kind": "final", "question": None, "payload": payload})
    return _gemini_body(text)


def _has_key_anywhere(obj, key: str) -> bool:
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(_has_key_anywhere(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key_anywhere(v, key) for v in obj)
    return False


class TestStartSessionFinal:
    async def test_kind_is_final(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session("sys", "initial user message")
        assert reply.kind == "final"
        assert isinstance(handle, OpenAICompatSessionHandle)

    async def test_api_called_against_model_specific_url(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend(model="gemini-3.6-flash")
            await backend.start_session("sys", "hi")
        call_args = mock_client.post.call_args
        assert call_args.args[0] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent"
        )

    async def test_api_called_with_x_goog_api_key_header_not_bearer(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "secret-key")
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["x-goog-api-key"] == "secret-key"
        assert "Authorization" not in headers

    async def test_env_var_fallback_to_google_api_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_API_KEY", "fallback-key")
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["x-goog-api-key"] == "fallback-key"

    async def test_system_prompt_sent_as_system_instruction(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("system prompt text", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["systemInstruction"] == {"parts": [{"text": "system prompt text"}]}
        assert "system" not in [m.get("role") for m in payload["contents"]]

    async def test_initial_user_message_sent_as_user_content(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hello there")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["contents"] == [{"role": "user", "parts": [{"text": "hello there"}]}]


class TestRestoreSession:
    async def test_no_api_call_is_made(self):
        with patch("httpx.AsyncClient") as mock_ctor:
            backend = GeminiBackend()
            handle = await backend.restore_session(
                "sys", [HistoryTurn(role="user", content="hi")], external_id=None
            )
        mock_ctor.assert_not_called()
        assert isinstance(handle, OpenAICompatSessionHandle)


class TestSendMessage:
    async def test_assistant_role_mapped_to_model_on_next_call(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, _ = await backend.start_session("sys", "first")
            await backend.send_message(handle, "second")
        payload = mock_client.post.call_args.kwargs["json"]
        roles = [c["role"] for c in payload["contents"]]
        assert roles == ["user", "model", "user"]

    async def test_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle

        backend = GeminiBackend()
        with pytest.raises(TypeError):
            await backend.send_message(SessionHandle(id="x"), "hi")


class TestEndSession:
    async def test_messages_cleared(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, _ = await backend.start_session("sys", "hi")
            await backend.end_session(handle)
        assert handle.messages == []


class TestCallApiTimeout:
    async def test_timeout_raises_agent_timeout(self):
        import httpx as httpx_module

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "hi")


class TestCallApiErrors:
    async def test_http_429_raises_agent_limit_reached(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_bad_model_4xx_raises_agent_backend_unavailable(self):
        mock_client = _make_mock_client(
            {"error": {"code": 404, "message": "model not found", "status": "NOT_FOUND"}},
            status_code=404,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")

    async def test_resource_exhausted_status_raises_agent_limit_reached(self):
        mock_client = _make_mock_client(
            {"error": {"code": 429, "message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
            status_code=429,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_no_candidates_raises_backend_unavailable_after_retries(self):
        mock_client = _make_mock_client({"candidates": []})
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 3  # _MAX_ATTEMPTS


class TestCallApiRetryClassification:
    async def test_transient_5xx_then_success_retries_same_backend(self):
        mock_client = MagicMock()
        bad_response = MagicMock(status_code=502)
        bad_response.json = MagicMock(side_effect=ValueError())
        bad_response.text = "Bad Gateway"
        good_response = MagicMock(status_code=200)
        good_response.json = MagicMock(return_value=_gemini_body(FINAL_RAW))
        good_response.text = "ok"
        mock_client.post = AsyncMock(side_effect=[bad_response, good_response])
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 2

    async def test_rate_limit_switches_immediately_no_retry(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 1


class TestMaxTokensTruncation:
    async def test_sentinel_mode_max_tokens_raises_protocol_error(self):
        mock_client = _make_mock_client(_gemini_body("partial te", finish_reason="MAX_TOKENS"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(ProtocolError, match="truncated"):
                await backend.start_session("sys", "hi")

    async def test_structured_mode_max_tokens_raises_protocol_error(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = _make_mock_client(_gemini_body('{"kind":', finish_reason="MAX_TOKENS"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(ProtocolError, match="truncated"):
                await backend.start_session("sys", "hi", structured_schema=schema)


class TestNudgeOnMissingSentinel:
    async def test_missing_sentinel_then_nudge_succeeds(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(NO_SENTINEL_RAW)), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 2

    async def test_missing_sentinel_twice_propagates_protocol_error(self):
        mock_client = _make_mock_client(_gemini_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(ProtocolError):
                await backend.start_session("sys", "hi")


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class TestSupportsStructuredOutput:
    def test_flag_is_true(self):
        assert GeminiBackend().supports_structured_output is True


class TestStructuredRequestShape:
    async def test_response_schema_sent_and_inlined_when_schema_given(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = _make_mock_client(_structured_final_body(_cv_payload()))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi", structured_schema=schema)
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        sent_schema = payload["generationConfig"]["responseSchema"]
        assert not _has_key_anywhere(sent_schema, "$defs")
        assert not _has_key_anywhere(sent_schema, "$ref")
        assert not _has_key_anywhere(sent_schema, "additionalProperties")

    async def test_no_schema_fields_in_generation_config_when_schema_none(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "responseSchema" not in payload["generationConfig"]
        assert "responseMimeType" not in payload["generationConfig"]

    async def test_max_output_tokens_always_pinned(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["generationConfig"]["maxOutputTokens"] == 32000

        schema = json_schema_for(Stage.cv_adjust)
        mock_client2 = _make_mock_client(_structured_final_body(_cv_payload()))
        with patch("httpx.AsyncClient", return_value=mock_client2):
            backend2 = GeminiBackend()
            await backend2.start_session("sys", "hi", structured_schema=schema)
        payload2 = mock_client2.post.call_args.kwargs["json"]
        assert payload2["generationConfig"]["maxOutputTokens"] == 32000


class TestContentsRoleNormalization:
    async def test_restore_then_send_starting_with_assistant_row_merges_adjacent_roles(self):
        """A resumed session whose stored history happens to start with an
        assistant turn (or otherwise has adjacent same-role rows) must not emit
        two consecutive same-role `contents` entries — Gemini rejects that shape."""
        history = [
            HistoryTurn(role="assistant", content="earlier reply"),
            HistoryTurn(role="assistant", content="another earlier reply"),
            HistoryTurn(role="user", content="a question"),
        ]
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", history, external_id=None)
            await backend.send_message(handle, "next turn")
        payload = mock_client.post.call_args.kwargs["json"]
        roles = [c["role"] for c in payload["contents"]]
        # Two adjacent "model" rows collapsed into one content entry.
        assert roles.count("model") == 1
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1]

    async def test_adjacent_same_role_merges_into_multi_part_content(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, _ = await backend.start_session("sys", "first")
            # Manually inject an adjacent same-role turn to simulate a foreign row.
            handle.messages.append({"role": "assistant", "content": "extra assistant turn"})
            await backend.send_message(handle, "second")
        payload = mock_client.post.call_args.kwargs["json"]
        roles = [c["role"] for c in payload["contents"]]
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1]


class TestSchemaRejectionDowngrade:
    async def test_permanent_4xx_in_structured_mode_downgrades_and_retries(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(
                    status_code=400,
                    json=MagicMock(return_value={
                        "error": {"code": 400, "message": "Invalid responseSchema", "status": "INVALID_ARGUMENT"}
                    }),
                    text="bad schema",
                ),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="ok"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False  # isolate the schema downgrade from the reasoning degrade
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert handle.structured_enabled is False
        assert mock_client.post.await_count == 2
        # Second (retried) call must not carry schema fields.
        retried_payload = mock_client.post.call_args.kwargs["json"]
        assert "responseSchema" not in retried_payload["generationConfig"]

    async def test_permanent_4xx_sheds_thinking_config_before_downgrading_the_schema(self):
        """thinkingConfig is an optional enrichment and not every Gemini model
        supports it, so a permanent 4xx drops it FIRST — losing the thinking stream
        rather than structured mode. Only if the clean retry also 4xxs does the
        schema downgrade fire."""
        schema = json_schema_for(Stage.cv_adjust)
        rejection = MagicMock(
            status_code=400,
            json=MagicMock(return_value={
                "error": {"code": 400, "message": "Invalid argument", "status": "INVALID_ARGUMENT"}
            }),
            text="bad request",
        )
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                rejection,
                MagicMock(status_code=200, json=MagicMock(return_value=_structured_final_body(_cv_payload())), text="ok"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert backend._reasoning is False
        # Structured mode survived — only the thinking opt-in was shed.
        assert handle.structured_enabled is True
        assert mock_client.post.await_count == 2
        retried_payload = mock_client.post.call_args.kwargs["json"]
        assert "thinkingConfig" not in retried_payload["generationConfig"]
        assert "responseSchema" in retried_payload["generationConfig"]

    async def test_permanent_4xx_in_sentinel_mode_raises_backend_unavailable_no_retry(self):
        mock_client = _make_mock_client(
            {"error": {"code": 400, "message": "bad request", "status": "INVALID_ARGUMENT"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 1

    async def test_schema_rejection_during_send_message_downgrades(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_structured_final_body(_cv_payload())), text="ok"),
                MagicMock(
                    status_code=400,
                    json=MagicMock(return_value={
                        "error": {"code": 400, "message": "Invalid responseSchema", "status": "INVALID_ARGUMENT"}
                    }),
                    text="bad schema",
                ),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="ok2"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, _ = await backend.start_session("sys", "hi", structured_schema=schema)
            reply = await backend.send_message(handle, "next", structured_schema=schema)
        assert reply.kind == "final"
        assert handle.structured_enabled is False
        assert mock_client.post.await_count == 3


class TestDowngradeOnUnparseableStructuredReply:
    async def test_non_json_reply_downgrades_and_nudge_recovers(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body("not json")), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert handle.structured_enabled is False

    async def test_downgrade_persists_to_subsequent_send_message_calls(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body("not json")), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="y"),
                MagicMock(status_code=200, json=MagicMock(return_value=_gemini_body(FINAL_RAW)), text="z"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, _ = await backend.start_session("sys", "hi", structured_schema=schema)
            await backend.send_message(handle, "next", structured_schema=schema)
        last_payload = mock_client.post.call_args.kwargs["json"]
        assert "responseSchema" not in last_payload["generationConfig"]


class TestCachedTokenObservability:
    """Phase 3 of the prompt-caching plan: Gemini's implicit caching needs no
    request change, only visibility into usageMetadata.cachedContentTokenCount."""

    async def test_cached_tokens_logged_when_present(self, caplog):
        body = _gemini_body(FINAL_RAW)
        body["usageMetadata"] = {"cachedContentTokenCount": 1234}
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level("INFO"):
                backend = GeminiBackend()
                await backend.start_session("sys", "hi")
        assert any("cachedContentTokenCount=1234" in r.message for r in caplog.records)

    async def test_missing_usage_metadata_does_not_raise(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"

    async def test_wrong_typed_usage_metadata_does_not_raise(self):
        body = _gemini_body(FINAL_RAW)
        body["usageMetadata"] = "not-a-dict"
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"


def _gemini_sse_lines(events: list[dict]) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events]


def _gemini_stream_event(parts: list[dict], finish_reason: str | None = None) -> dict:
    candidate: dict = {"content": {"parts": parts}}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return {"candidates": [candidate]}


def _make_mock_stream_client(lines: list[str], status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code

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
    mock_client.post = AsyncMock()
    mock_client.aclose = AsyncMock()
    return mock_client


class TestGeminiStreaming:
    """The regression this class exists for: `_call_api_once` used to gate streaming
    on `structured_schema is None`. Since `_structured_schema_for` hands this backend
    a schema unconditionally, that made every real pipeline call fall to the buffered
    `generateContent` endpoint and nothing ever streamed — no thinking, ever."""

    async def test_structured_mode_still_streams(self):
        payload_json = json.dumps({"kind": "final", "question": None, "payload": _cv_payload()})
        lines = _gemini_sse_lines([
            _gemini_stream_event([{"text": "mapping the JD onto the CV", "thought": True}]),
            _gemini_stream_event([{"text": payload_json}], finish_reason="STOP"),
        ])
        mock_client = _make_mock_stream_client(lines)
        received: list = []

        async def on_chunk(chunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle, reply = await backend.start_session(
                "sys", "hi", structured_schema=json_schema_for(Stage.cv_adjust), on_chunk=on_chunk
            )

        assert mock_client.stream.called
        mock_client.post.assert_not_awaited()
        assert reply.kind == "final"
        assert handle.structured_enabled is True
        url = mock_client.stream.call_args.args[1]
        assert url.endswith(":streamGenerateContent")

    async def test_structured_mode_forwards_thought_parts_but_suppresses_partial_json(self):
        """`_call_api` wraps on_chunk with `_reasoning_only` while structured, so the
        raw partial JSON never leaks into the chat UI — only the thought summary."""
        payload_json = json.dumps({"kind": "final", "question": None, "payload": _cv_payload()})
        lines = _gemini_sse_lines([
            _gemini_stream_event([{"text": "weighing options", "thought": True}]),
            _gemini_stream_event([{"text": payload_json}], finish_reason="STOP"),
        ])
        mock_client = _make_mock_stream_client(lines)
        received: list = []

        async def on_chunk(chunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session(
                "sys", "hi", structured_schema=json_schema_for(Stage.cv_adjust), on_chunk=on_chunk
            )

        assert [(c.kind, c.text) for c in received] == [("reasoning", "weighing options")]

    async def test_sentinel_mode_streams_both_kinds_unfiltered(self):
        lines = _gemini_sse_lines([
            _gemini_stream_event([{"text": "thinking out loud", "thought": True}]),
            _gemini_stream_event([{"text": FINAL_RAW}], finish_reason="STOP"),
        ])
        mock_client = _make_mock_stream_client(lines)
        received: list = []

        async def on_chunk(chunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            _, reply = await backend.start_session("sys", "hi", on_chunk=on_chunk)

        assert reply.kind == "final"
        assert [(c.kind, c.text) for c in received] == [
            ("reasoning", "thinking out loud"),
            ("content", FINAL_RAW),
        ]

    async def test_no_on_chunk_still_uses_the_buffered_endpoint(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 1
        assert mock_client.post.call_args.args[0].endswith(":generateContent")


class TestGeminiThinkingConfig:
    async def test_thinking_config_sent_by_default(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        config = mock_client.post.call_args.kwargs["json"]["generationConfig"]
        assert config["thinkingConfig"] == {"includeThoughts": True}

    async def test_thinking_config_omitted_once_degraded(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False
            await backend.start_session("sys", "hi")
        config = mock_client.post.call_args.kwargs["json"]["generationConfig"]
        assert "thinkingConfig" not in config


# ---------------------------------------------------------------------------
# Native tool calling (revision-tool-use plan, E4)
#
# Gemini's wire shape for tools is `tools: [{"functionDeclarations": [...]}]` +
# `toolConfig.functionCallingConfig.mode = "ANY"`, and its replies carry
# `functionCall` parts with NO text part at all — the trap the plan names as
# finding #3, since `_call_api_once`'s `if not text` raise sits right where such a
# reply lands. Everything else (session handling, parsing, the rung ladder) is
# inherited from OpenAICompatBackend and covered in test_openai_compat.py.
# ---------------------------------------------------------------------------


def _cv_specs() -> tuple:
    return tools_for(Stage.revising_cv)


def _function_call_part(name: str, args: dict | None) -> dict:
    part: dict = {"functionCall": {"name": name}}
    if args is not None:
        part["functionCall"]["args"] = args
    return part


def _function_call_body(*parts: dict, finish_reason: str = "STOP") -> dict:
    """A Gemini reply carrying only functionCall parts — no text part whatsoever,
    which is exactly what a real forced tool call looks like on this API."""
    return {"candidates": [{"content": {"parts": list(parts)}, "finishReason": finish_reason}]}


def _4xx_response(message: str = "Invalid argument") -> MagicMock:
    return MagicMock(
        status_code=400,
        json=MagicMock(return_value={
            "error": {"code": 400, "message": message, "status": "INVALID_ARGUMENT"}
        }),
        text="bad request",
    )


class TestSupportsNativeTools:
    def test_flag_is_inherited_true_from_the_shared_base(self):
        assert GeminiBackend.supports_native_tools is True

    def test_flag_is_true_on_an_instance(self):
        assert GeminiBackend().supports_native_tools is True


class TestGeminiToolRequestShape:
    async def test_function_declarations_and_any_mode_sent(self):
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session(
                "sys", [HistoryTurn(role="user", content="hi")], None, tools=_cv_specs()
            )
            await backend.send_message(handle, "shorten the summary")
        payload = mock_client.post.call_args.kwargs["json"]
        declarations = payload["tools"][0]["functionDeclarations"]
        names = [d["name"] for d in declarations]
        assert "get_cv" in names and "finalize" in names
        assert all("parameters" in d and "description" in d for d in declarations)
        assert payload["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}

    async def test_no_response_schema_when_tools_active(self):
        """Tool mode and structured mode are mutually exclusive per request — the
        terminal tool's arguments ARE the structured output."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
        config = mock_client.post.call_args.kwargs["json"]["generationConfig"]
        assert "responseSchema" not in config
        assert "responseMimeType" not in config

    async def test_declarations_carry_no_additional_properties_or_refs(self):
        """Gemini's restricted OpenAPI-subset schema rejects all three. The renderer
        strips additionalProperties and the specs are hand-written $ref-free — assert
        it here rather than trusting the docstring, so a future tool_spec.py edit that
        reintroduces a $ref fails loudly instead of silently 400ing on the wire."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
        declarations = mock_client.post.call_args.kwargs["json"]["tools"]
        assert not _has_key_anywhere(declarations, "additionalProperties")
        assert not _has_key_anywhere(declarations, "$ref")
        assert not _has_key_anywhere(declarations, "$defs")

    async def test_both_tools_and_schema_on_one_request_is_an_assertion_error(self):
        """The mutual exclusion is asserted, not merely arranged for by the callers."""
        backend = GeminiBackend()
        with pytest.raises(AssertionError, match="mutually exclusive"):
            await backend._call_api_once(
                "sys",
                [{"role": "user", "content": "hi"}],
                structured_schema=json_schema_for(Stage.cv_adjust),
                tools=_cv_specs(),
            )

    async def test_no_tools_keys_when_tools_absent(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "tools" not in payload and "toolConfig" not in payload

    async def test_tool_mode_never_streams(self):
        """Plan finding #1: an on_chunk-carrying call still goes down the buffered
        generateContent path once tools are attached — which is what makes
        _consume_gemini_sse's lack of functionCall handling correct, not a gap."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        mock_client.stream = MagicMock()

        async def _on_chunk(_chunk):
            return None

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go", on_chunk=_on_chunk)
        mock_client.stream.assert_not_called()
        assert reply.kind == "tool_calls"
        assert mock_client.post.call_args.args[0].endswith(":generateContent")


class TestGeminiToolReplyExtraction:
    async def test_function_call_only_reply_is_not_a_transient_empty_reply(self):
        """The sharpest trap in E4 (plan finding #3): a functionCall-only reply has NO
        text part, so `if not text: raise TransientBackendError` would fire first,
        burn every retry attempt and end as AgentBackendUnavailable — dropping the
        backend out of BF-19 over a perfectly good tool call."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.kind == "tool_calls"
        assert mock_client.post.await_count == 1  # exactly one HTTP attempt, no retries

    async def test_all_function_calls_returned_in_order_with_positional_ids(self):
        """Gemini issues no call ids, and a turn can carry several parallel calls —
        returning only the first is a named bug class in the plan (finding #5)."""
        mock_client = _make_mock_client(
            _function_call_body(
                _function_call_part("get_cv", {}),
                _function_call_part("edit_entry_bullets", {"entry_id": "e1", "bullets": ["one"]}),
                _function_call_part("finalize", {"change_log": "tightened"}),
            )
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert [c.name for c in reply.tool_calls] == ["get_cv", "edit_entry_bullets", "finalize"]
        assert [c.id for c in reply.tool_calls] == ["call_0", "call_1", "call_2"]
        assert reply.tool_calls[1].arguments == {"entry_id": "e1", "bullets": ["one"]}
        assert all(isinstance(c.arguments, dict) for c in reply.tool_calls)

    async def test_ids_are_keyed_on_block_position_not_call_count(self):
        """Mirrors anthropic_api.py's identical choice: keying on the block index means
        two calls can never collide on one id even when other parts are interleaved."""
        mock_client = _make_mock_client(
            _function_call_body(
                {"text": "let me look first"},
                _function_call_part("get_cv", {}),
            )
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert [c.id for c in reply.tool_calls] == ["call_1"]

    async def test_missing_args_becomes_an_empty_dict(self):
        """A zero-parameter tool (get_cv) legitimately comes back with no `args` key."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", None)))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.tool_calls[0].arguments == {}

    async def test_text_reply_in_tool_mode_is_not_sentinel_nudged(self):
        """mode "ANY" should prevent this, but a model answering prose anyway must not
        trigger the sentinel nudge (a tool session was never given that contract) —
        one attempt, handed back for tool_loop.py's rung 3."""
        mock_client = _make_mock_client(_gemini_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 1  # no nudge replay

    async def test_function_call_part_is_ignored_when_no_tools_were_attached(self):
        """Extraction is gated on `tools`: ungated, a stray functionCall in a non-tool
        session would hand run_stage a kind="tool_calls" reply for a cv_adjust turn."""
        mock_client = _make_mock_client(_function_call_body(_function_call_part("get_cv", {})))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 3  # unchanged empty-reply transient path


class TestGeminiSendToolResults:
    @staticmethod
    def _two_round_client(second_body: dict) -> MagicMock:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_function_call_body(
                    _function_call_part("get_cv", {}),
                    _function_call_part("remove_entry", {"entry_id": "e9"}),
                ))),
                MagicMock(status_code=200, text="y", json=MagicMock(return_value=second_body)),
            ]
        )
        mock_client.aclose = AsyncMock()
        return mock_client

    @staticmethod
    def _results() -> list:
        return [
            ToolResult(call_id="call_0", name="get_cv", ok=True, content={"ok": True, "cv": {}}),
            ToolResult(call_id="call_1", name="remove_entry", ok=False,
                       content={"ok": False, "error": {"code": "bad_argument"}}),
        ]

    async def test_function_response_parts_follow_the_function_call_turn_in_order(self):
        mock_client = self._two_round_client(
            _function_call_body(_function_call_part("finalize", {"change_log": "done"}))
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            first = await backend.send_message(handle, "go")
            second = await backend.send_tool_results(handle, self._results())
        assert first.kind == "tool_calls" and second.kind == "tool_calls"
        contents = mock_client.post.call_args.kwargs["json"]["contents"]
        assert [c["role"] for c in contents] == ["user", "model", "user"]
        # The model turn replays the calls it made, as functionCall parts.
        assert [p["functionCall"]["name"] for p in contents[1]["parts"]] == ["get_cv", "remove_entry"]
        assert contents[1]["parts"][1]["functionCall"]["args"] == {"entry_id": "e9"}
        # The results come back as functionResponse parts on a USER turn (there is no
        # "tool"/"function" role on this API), one per result, in the same order.
        responses = [p["functionResponse"] for p in contents[2]["parts"]]
        assert [r["name"] for r in responses] == ["get_cv", "remove_entry"]
        assert responses[0]["response"] == {"ok": True, "cv": {}}
        assert responses[1]["response"] == {"ok": False, "error": {"code": "bad_argument"}}

    async def test_tools_still_attached_on_the_follow_up_request(self):
        mock_client = self._two_round_client(
            _function_call_body(_function_call_part("finalize", {"change_log": "done"}))
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            await backend.send_tool_results(handle, self._results())
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["tools"][0]["functionDeclarations"]
        assert payload["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
        assert "responseSchema" not in payload["generationConfig"]

    async def test_text_reply_to_results_ends_tool_mode_and_clears_pending(self):
        mock_client = self._two_round_client(_gemini_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            reply = await backend.send_tool_results(handle, self._results())
        assert reply.kind == "final"
        assert handle.pending_tool_calls is None
        assert mock_client.post.await_count == 2  # no nudge replay
        assert [m["role"] for m in handle.messages] == ["user", "assistant", "user", "assistant"]

    async def test_non_dict_result_content_is_wrapped_in_an_object(self):
        """functionResponse.response must be a JSON object on this wire shape."""
        mock_client = self._two_round_client(
            _function_call_body(_function_call_part("finalize", {"change_log": "d"}))
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            await backend.send_tool_results(
                handle, [ToolResult(call_id="call_0", name="get_cv", ok=True, content="plain text")]
            )
        contents = mock_client.post.call_args.kwargs["json"]["contents"]
        assert contents[-1]["parts"][0]["functionResponse"]["response"] == {"result": "plain text"}

    async def test_handle_is_not_mutated_when_the_follow_up_call_fails(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_function_call_body(
                    _function_call_part("get_cv", {})))),
                *[MagicMock(status_code=500, text="boom", json=MagicMock(return_value={})) for _ in range(3)],
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            before = list(handle.messages)
            pending_before = handle.pending_tool_calls
            with pytest.raises(AgentBackendUnavailable):
                await backend.send_tool_results(
                    handle, [ToolResult(call_id="call_0", name="get_cv", ok=True, content={"ok": True})]
                )
        assert handle.messages == before
        assert handle.pending_tool_calls == pending_before

    async def test_wrong_handle_type_raises(self):
        backend = GeminiBackend()
        with pytest.raises(TypeError):
            await backend.send_tool_results(object(), [])  # type: ignore[arg-type]

    async def test_handle_without_tools_raises_value_error(self):
        """A caller bug must fail loudly here rather than 400ing on the wire, where it
        would be misread as a provider rejection and cost the turn its native rung."""
        backend = GeminiBackend()
        handle = await backend.restore_session("sys", [], None)
        with pytest.raises(ValueError, match="native tool session"):
            await backend.send_tool_results(
                handle, [ToolResult(call_id="call_0", name="get_cv", ok=True, content={"ok": True})]
            )

    async def test_tools_rejected_mid_loop_is_a_tools_unsupported(self):
        """tool_loop.py wraps this exact call in `except ToolsUnsupported`, so
        send_tool_results really has to be able to produce one — not just send_message."""
        mock_client = self._two_round_client({})
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_function_call_body(
                    _function_call_part("get_cv", {})))),
                _4xx_response("Function calling is not supported for this model"),
            ]
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            with pytest.raises(ToolsUnsupported) as exc_info:
                await backend.send_tool_results(
                    handle, [ToolResult(call_id="call_0", name="get_cv", ok=True, content={"ok": True})]
                )
        assert isinstance(exc_info.value, _ToolsRejected)
        assert not isinstance(exc_info.value, AgentBackendUnavailable)
        assert mock_client.post.await_count == 2  # no in-backend retry-clean


class TestGeminiToolsRejectedDegrade:
    """A permanent 4xx with declarations on the wire is a TOOLS rejection, not a dead
    backend: tool_loop.py's native->prompt rung ladder owns the recovery, so this
    backend must neither retry clean in-process (as the reasoning/schema degrades do)
    nor tell BF-19 to advance the whole job."""

    async def test_permanent_4xx_with_tools_raises_tools_unsupported(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_4xx_response("Unsupported field: tools"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(ToolsUnsupported) as exc_info:
                await backend.send_message(handle, "go")
        assert isinstance(exc_info.value, _ToolsRejected)
        # NOT an AgentBackendUnavailable — an escaped instance must never be read as a
        # BF-19 signal (jsa/agents/base.py::ToolsUnsupported).
        assert not isinstance(exc_info.value, AgentBackendUnavailable)
        assert mock_client.post.await_count == 1  # one attempt: no retry, no degrade loop

    async def test_tools_shed_before_reasoning(self):
        """Degrade precedence is tools -> reasoning -> caching -> fail. thinkingConfig
        is on by default, so this pins that tools shed FIRST rather than the request
        being retried once clean without thinking."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_4xx_response())
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            assert backend._reasoning is True
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(_ToolsRejected):
                await backend.send_message(handle, "go")
        assert mock_client.post.await_count == 1
        assert backend._reasoning is True  # the reasoning degrade never fired

    async def test_permanent_4xx_without_tools_is_unchanged_schema_rejection(self):
        """The pre-existing behavior must be untouched for a non-tools call: a
        structured-mode 4xx is still _SchemaRejected (an AgentBackendUnavailable that
        start_session/send_message downgrade), never a ToolsUnsupported."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_4xx_response("Invalid responseSchema"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False  # isolate from the reasoning degrade
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend._call_api_once(
                    "sys",
                    [{"role": "user", "content": "hi"}],
                    structured_schema=json_schema_for(Stage.cv_adjust),
                )
        assert not isinstance(exc_info.value, ToolsUnsupported)

    async def test_permanent_4xx_without_tools_in_sentinel_mode_is_still_bf19(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=_4xx_response())
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend.start_session("sys", "hi")
        assert not isinstance(exc_info.value, ToolsUnsupported)
        assert mock_client.post.await_count == 1

    async def test_429_with_tools_present_is_still_a_limit_signal(self):
        """Quota is an account-scoped constraint — it must reach BF-19 as
        AgentLimitReached, not be misread as a tools rejection."""
        mock_client = _make_mock_client(
            {"error": {"code": 429, "message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}},
            status_code=429,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(AgentLimitReached):
                await backend.send_message(handle, "go")
