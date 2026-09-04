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

from jsa.agents._openai_compat import OpenAICompatSessionHandle
from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached, AgentTimeout, HistoryTurn
from jsa.agents.gemini_api import GeminiBackend, _MAX_OUTPUT_TOKENS, _THINKING_BUDGET
from jsa.agents.protocol import ProtocolError
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
        assert config["thinkingConfig"] == {
            "includeThoughts": True,
            "thinkingBudget": _THINKING_BUDGET,
        }

    async def test_thinking_budget_reserves_room_for_the_reply(self):
        """Regression pin: the budget must stay strictly below the shared output
        ceiling, so a fixed floor of _MAX_OUTPUT_TOKENS is always reserved for the
        CV/cover-letter JSON regardless of how much the model wants to think — see
        _reasoning_payload's docstring for the live-observed failure (a schema-valid
        CV with a Summary section only and no Experience content) this guards
        against. Do not "simplify" this back to an unset/dynamic thinkingBudget."""
        assert 0 < _THINKING_BUDGET < _MAX_OUTPUT_TOKENS

    async def test_thinking_config_omitted_once_degraded(self):
        mock_client = _make_mock_client(_gemini_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = GeminiBackend()
            backend._reasoning = False
            await backend.start_session("sys", "hi")
        config = mock_client.post.call_args.kwargs["json"]["generationConfig"]
        assert "thinkingConfig" not in config
