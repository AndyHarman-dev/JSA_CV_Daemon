"""Tests for jsa/agents/_openai_compat.py's shared OpenAICompatBackend machinery.

Exercised via MistralBackend — the purest concrete subclass (no _extra_payload
override, no protocol dispatch) — so these tests cover the shared base directly.
OpenRouter's routing-guard payload addition and OpenCode-GO's dual-protocol
dispatch get their own dedicated test files (test_openrouter.py, test_opencode_go.py).

Mirrors tests/backend/test_opencode_zen.py's structure and fixtures; see that
file's module docstring for the httpx.AsyncClient patching rationale.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import OpenAICompatSessionHandle
from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached, AgentTimeout, HistoryTurn
from jsa.agents.mistral import MistralBackend
from jsa.agents.protocol import ProtocolError
from jsa.db.models import Stage
from jsa.schema.turn_models import json_schema_for


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """retry_transient's backoff uses real asyncio.sleep — replace with a no-op."""
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"
NEED_INPUT_RAW = "<<<NEED_INPUT>>>\nWhat is your target industry?\n<<<END>>>"
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


def _completion_body(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class TestStartSessionFinal:
    async def test_kind_is_final(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "initial user message")
        assert reply.kind == "final"
        assert isinstance(handle, OpenAICompatSessionHandle)

    async def test_api_called_against_correct_endpoint(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client) as mock_ctor:
            backend = MistralBackend()
            await backend.start_session("sys", "hi")
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "https://api.mistral.ai/v1/chat/completions"

    async def test_api_called_with_bearer_auth_header(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "secret-key")
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("sys", "hi")
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret-key"

    async def test_api_called_with_model(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(model="mistral-large-2512")
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["model"] == "mistral-large-2512"


class TestRestoreSession:
    async def test_no_api_call_is_made(self):
        with patch("httpx.AsyncClient") as mock_ctor:
            backend = MistralBackend()
            handle = await backend.restore_session(
                "sys", [HistoryTurn(role="user", content="hi")], external_id=None
            )
        mock_ctor.assert_not_called()
        assert isinstance(handle, OpenAICompatSessionHandle)

    async def test_messages_reconstructed_from_history(self):
        backend = MistralBackend()
        history = [
            HistoryTurn(role="user", content="hi"),
            HistoryTurn(role="assistant", content="hello"),
        ]
        handle = await backend.restore_session("sys", history, external_id=None)
        assert handle.messages == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]


class TestSendMessage:
    async def test_handle_has_four_messages_after_send(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, _ = await backend.start_session("sys", "first")
            await backend.send_message(handle, "second")
        assert len(handle.messages) == 4

    async def test_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle

        backend = MistralBackend()
        with pytest.raises(TypeError):
            await backend.send_message(SessionHandle(id="x"), "hi")


class TestEndSession:
    async def test_messages_cleared(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, _ = await backend.start_session("sys", "hi")
            await backend.end_session(handle)
        assert handle.messages == []

    async def test_raises_type_error_for_wrong_handle(self):
        from jsa.agents.base import SessionHandle

        backend = MistralBackend()
        with pytest.raises(TypeError, match="OpenAICompatSessionHandle"):
            await backend.end_session(SessionHandle(id="x"))


class TestCallApiTimeout:
    async def test_timeout_raises_agent_timeout(self):
        import httpx as httpx_module

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "hi")


class TestCallApiErrors:
    async def test_http_429_raises_agent_limit_reached(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")

    async def test_bad_model_4xx_raises_agent_backend_unavailable(self):
        mock_client = _make_mock_client(
            {"error": {"message": "invalid model", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")

    async def test_null_content_raises_backend_unavailable_after_retries(self):
        mock_client = _make_mock_client(_completion_body(None))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
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
        good_response.json = MagicMock(return_value=_completion_body(FINAL_RAW))
        good_response.text = "ok"
        mock_client.post = AsyncMock(side_effect=[bad_response, good_response])
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 2

    async def test_rate_limit_switches_immediately_no_retry(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            with pytest.raises(AgentLimitReached):
                await backend.start_session("sys", "hi")
        assert mock_client.post.await_count == 1


class TestNudgeOnMissingSentinel:
    async def test_missing_sentinel_then_nudge_succeeds(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(NO_SENTINEL_RAW)), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 2

    async def test_missing_sentinel_twice_propagates_protocol_error(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=MagicMock(
                status_code=200, json=MagicMock(return_value=_completion_body(NO_SENTINEL_RAW)), text="x"
            )
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            with pytest.raises(ProtocolError):
                await backend.start_session("sys", "hi")


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------

def _cv_payload() -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [{"name": "Summary", "text": "Senior engineer."}],
    }


def _structured_final_body(payload: dict) -> dict:
    import json

    text = json.dumps({"kind": "final", "question": None, "payload": payload})
    return _completion_body(text)


class TestSupportsStructuredOutput:
    def test_flag_is_true(self):
        assert MistralBackend().supports_structured_output is True


class TestStructuredRequestShape:
    async def test_response_format_sent_when_schema_given(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = _make_mock_client(_structured_final_body(_cv_payload()))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("sys", "hi", structured_schema=schema)
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["response_format"]["json_schema"]["strict"] is False
        assert payload["response_format"]["json_schema"]["schema"] == schema

    async def test_no_response_format_when_schema_none(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "response_format" not in payload


class TestStructuredQuestionKind:
    """TestStartSessionFinal only exercises kind='final' — a structured reply
    asking a clarifying question is the other half of CvTurn/ClTurn's union and
    must parse to needs_input without touching the downgrade path."""

    async def test_question_kind_parses_as_needs_input(self):
        import json

        schema = json_schema_for(Stage.cv_adjust)
        text = json.dumps({"kind": "question", "question": "What is your target industry?", "payload": None})
        mock_client = _make_mock_client(_completion_body(text))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "needs_input"
        assert reply.question == "What is your target industry?"
        assert handle.structured_enabled is True


class TestFitVerdictStructuredParse:
    """fit_assessment's structured shape (FitVerdict) has no kind/question/payload
    union at all — it's routed by parse_structured_reply_for_schema's is_fit branch
    keyed off the schema itself, not a Stage enum the backend never sees."""

    async def test_fit_verdict_parses_as_final(self):
        import json

        schema = json_schema_for(Stage.fit_assessment)
        text = json.dumps({"verdict": "FIT", "reason": "Strong match on required skills."})
        mock_client = _make_mock_client(_completion_body(text))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert reply.content == "FIT\nStrong match on required skills."
        assert handle.structured_enabled is True


class TestSemanticFailureDoesNotDowngrade:
    """CLAUDE.md's invariant: parse_structured_reply_for_schema does json.loads +
    kind routing ONLY, never CvTurn/CVDocument model validation — that stays
    stage-side in stages.py's _validate_final_content. So a reply whose payload
    would fail that later Pydantic validation (missing every real CV field) must
    still parse cleanly here and must NOT trip the unparseable-reply downgrade."""

    async def test_semantically_empty_payload_still_parses_and_stays_structured(self):
        import json

        schema = json_schema_for(Stage.cv_adjust)
        # {} is a dict, so the payload-presence check passes even though it would
        # fail CVDocument's own required-field validation one layer up.
        text = json.dumps({"kind": "final", "question": None, "payload": {}})
        mock_client = _make_mock_client(_completion_body(text))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert handle.structured_enabled is True  # no downgrade on a semantic gap


class TestPromptCacheKey:
    """Phase 2 of the prompt-caching plan: MistralBackend._extra_payload derives a
    stable prompt_cache_key from the system prompt so requests sharing the same
    byte-identical system prefix (see CLAUDE.md -> "Prompt caching") route to a
    server that already holds it."""

    async def test_prompt_cache_key_present_by_default(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("shared system prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["prompt_cache_key"].startswith("jsa-")

    async def test_prompt_cache_key_stable_across_jobs_sharing_a_system_prompt(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            await MistralBackend().start_session("shared system prompt", "job A's JD")
            first_key = mock_client.post.call_args.kwargs["json"]["prompt_cache_key"]
            await MistralBackend().start_session("shared system prompt", "job B's JD")
            second_key = mock_client.post.call_args.kwargs["json"]["prompt_cache_key"]
        assert first_key == second_key

    async def test_prompt_cache_key_differs_for_different_system_prompts(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            await MistralBackend().start_session("system prompt A", "hi")
            key_a = mock_client.post.call_args.kwargs["json"]["prompt_cache_key"]
            await MistralBackend().start_session("system prompt B", "hi")
            key_b = mock_client.post.call_args.kwargs["json"]["prompt_cache_key"]
        assert key_a != key_b

    async def test_prompt_caching_false_is_byte_identical_to_pre_phase2_payload(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(prompt_caching=False)
            await backend.start_session("shared system prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload == {
            "model": backend._model,
            "messages": [
                {"role": "system", "content": "shared system prompt"},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": 32000,
        }

    async def test_cached_tokens_logged(self, caplog):
        body = _completion_body(FINAL_RAW)
        body["usage"] = {"prompt_tokens_details": {"cached_tokens": 512}}
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with caplog.at_level("INFO"):
                backend = MistralBackend()
                await backend.start_session("sys", "hi")
        assert "cached_tokens=512" in caplog.text

    async def test_missing_usage_does_not_raise(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            _, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"


class TestSystemContentHookDefault:
    """Phase 4's ``_system_content`` hook default must leave a subclass that never
    overrides it (Mistral: its caching signal is a top-level ``_extra_payload`` key,
    not a system-content shape change) byte-unchanged, even with prompt_caching on."""

    async def test_mistral_system_content_stays_a_plain_string(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(prompt_caching=True)
            await backend.start_session("shared system prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["messages"][0] == {"role": "system", "content": "shared system prompt"}


class TestDowngradeOnUnparseableStructuredReply:
    async def test_non_json_reply_downgrades_and_nudge_recovers(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body("not json")), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi", structured_schema=schema)
        assert reply.kind == "final"
        assert handle.structured_enabled is False

    async def test_downgrade_persists_to_subsequent_send_message_calls(self):
        schema = json_schema_for(Stage.cv_adjust)
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body("not json")), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="y"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="z"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, _ = await backend.start_session("sys", "hi", structured_schema=schema)
            await backend.send_message(handle, "next", structured_schema=schema)
        last_payload = mock_client.post.call_args.kwargs["json"]
        assert "response_format" not in last_payload
