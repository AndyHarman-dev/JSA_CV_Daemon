"""Tests for jsa/agents/_openai_compat.py's shared OpenAICompatBackend machinery.

Exercised via MistralBackend — the purest concrete subclass (no _extra_payload
override, no protocol dispatch) — so these tests cover the shared base directly.
OpenRouter's routing-guard payload addition and OpenCode-GO's dual-protocol
dispatch get their own dedicated test files (test_openrouter.py, test_opencode_go.py).

Mirrors tests/backend/test_opencode_zen.py's structure and fixtures; see that
file's module docstring for the httpx.AsyncClient patching rationale.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import (
    OpenAICompatBackend,
    OpenAICompatSessionHandle,
    _ToolsRejected,
)
from jsa.agents.base import (
    AgentBackendUnavailable,
    AgentLimitReached,
    AgentTimeout,
    HistoryTurn,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.mistral import MistralBackend
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import tools_for
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
        payload = dict(mock_client.post.call_args.kwargs["json"])
        # The prompt-caching kill switch's parity invariant is scoped to the
        # caching fields. `reasoning_effort` is a separate opt-in with its own
        # degrade path (see MistralBackend._reasoning_payload), so it is popped
        # here rather than being allowed to weaken this assertion.
        payload.pop("reasoning_effort", None)
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


# ---------------------------------------------------------------------------
# Native tool calling (revision-tool-use plan, Phase 3 / E2)
# ---------------------------------------------------------------------------


def _tool_call_entry(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _tool_calls_body(*entries: dict) -> dict:
    """An OpenAI-compatible reply whose message carries tool_calls and, as real
    providers do, a NULL content alongside them."""
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": list(entries)}}]}


def _cv_specs() -> tuple:
    return tools_for(Stage.revising_cv)


class TestSupportsNativeTools:
    def test_flag_is_true_on_the_shared_base(self):
        assert OpenAICompatBackend.supports_native_tools is True

    def test_flag_is_true_on_mistral(self):
        assert MistralBackend().supports_native_tools is True


class TestToolRequestShape:
    async def test_tools_and_tool_choice_sent_when_tools_given(self):
        mock_client = _make_mock_client(_tool_calls_body(_tool_call_entry("c1", "get_cv", "{}")))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session(
                "sys", [HistoryTurn(role="user", content="hi")], None, tools=_cv_specs()
            )
            await backend.send_message(handle, "shorten the summary")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["tool_choice"] == "required"
        names = [t["function"]["name"] for t in payload["tools"]]
        assert "get_cv" in names and "finalize" in names
        assert all(t["type"] == "function" for t in payload["tools"])

    async def test_no_response_format_when_tools_active(self):
        """Tool mode and structured mode are mutually exclusive per request — the
        terminal tool's arguments ARE the structured output."""
        mock_client = _make_mock_client(_tool_calls_body(_tool_call_entry("c1", "get_cv", "{}")))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "response_format" not in payload

    async def test_both_tools_and_schema_on_one_request_is_an_assertion_error(self):
        """The mutual exclusion is asserted, not merely arranged for by the callers."""
        backend = MistralBackend()
        with pytest.raises(AssertionError, match="mutually exclusive"):
            await backend._call_api_once(
                "sys",
                [{"role": "user", "content": "hi"}],
                structured_schema=json_schema_for(Stage.cv_adjust),
                tools=_cv_specs(),
            )

    async def test_no_tools_key_when_tools_absent(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert "tools" not in payload and "tool_choice" not in payload

    async def test_tool_mode_never_streams(self):
        """Plan Phase 2 finding #1: an on_chunk-carrying call still goes down the
        non-streaming path once tools are attached, which is what makes _consume_sse's
        lack of delta.tool_calls reassembly correct rather than a gap."""
        mock_client = _make_mock_client(_tool_calls_body(_tool_call_entry("c1", "get_cv", "{}")))
        mock_client.stream = MagicMock()

        async def _on_chunk(_chunk):
            return None

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go", on_chunk=_on_chunk)
        mock_client.stream.assert_not_called()
        assert "stream" not in mock_client.post.call_args.kwargs["json"]


class TestToolReplyExtraction:
    async def test_null_content_with_tool_calls_is_a_success_not_a_transient_retry(self):
        """The sharpest trap in E2: `content: null` alongside `tool_calls` must be
        read as a tool call, NOT as the null-content transient failure — otherwise a
        perfectly good call burns all _MAX_ATTEMPTS retries and ends as
        AgentBackendUnavailable, dropping the backend out of BF-19."""
        mock_client = _make_mock_client(_tool_calls_body(_tool_call_entry("c1", "get_cv", "{}")))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.kind == "tool_calls"
        assert mock_client.post.await_count == 1  # exactly one HTTP attempt

    async def test_multiple_tool_calls_returned_in_order_with_ids_and_parsed_arguments(self):
        mock_client = _make_mock_client(
            _tool_calls_body(
                _tool_call_entry("call_abc", "get_cv", "{}"),
                _tool_call_entry("call_def", "edit_entry_bullets",
                                 '{"entry_id": "e1", "bullets": ["one", "two"]}'),
                _tool_call_entry("call_ghi", "finalize", '{"change_log": "tightened"}'),
            )
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert [c.id for c in reply.tool_calls] == ["call_abc", "call_def", "call_ghi"]
        assert [c.name for c in reply.tool_calls] == ["get_cv", "edit_entry_bullets", "finalize"]
        assert reply.tool_calls[0].arguments == {}
        assert reply.tool_calls[1].arguments == {"entry_id": "e1", "bullets": ["one", "two"]}
        assert all(isinstance(c.arguments, dict) for c in reply.tool_calls)

    async def test_missing_provider_id_is_synthesized(self):
        entry = {"type": "function", "function": {"name": "get_cv", "arguments": "{}"}}
        mock_client = _make_mock_client(_tool_calls_body(entry))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.tool_calls[0].id == "call_0"

    async def test_malformed_arguments_json_raises_protocol_error(self):
        """A half-readable tool batch fails loudly rather than being silently coerced
        into a bogus kind='final' that stages.py would try to validate as a document."""
        mock_client = _make_mock_client(
            _tool_calls_body(_tool_call_entry("c1", "replace_summary", "{not json"))
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(ProtocolError, match="unparseable arguments JSON"):
                await backend.send_message(handle, "go")

    async def test_text_reply_in_tool_mode_is_not_sentinel_nudged(self):
        """tool_choice: 'required' should prevent this, but if a model answers prose
        anyway it must not trigger the sentinel nudge (a tool session was never given
        the sentinel contract) — one attempt, handed back for rung 3."""
        mock_client = _make_mock_client(_completion_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            reply = await backend.send_message(handle, "go")
        assert reply.kind == "final"
        assert mock_client.post.await_count == 1  # no nudge replay


class TestSendToolResults:
    async def test_one_tool_message_per_result_with_matching_ids(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c1", "get_cv", "{}"),
                    _tool_call_entry("c2", "remove_entry", '{"entry_id": "e9"}'),
                ))),
                MagicMock(status_code=200, text="y", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c3", "finalize", '{"change_log": "done"}'),
                ))),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            first = await backend.send_message(handle, "go")
            second = await backend.send_tool_results(
                handle,
                [
                    ToolResult(call_id="c1", name="get_cv", ok=True, content={"ok": True, "cv": {}}),
                    ToolResult(call_id="c2", name="remove_entry", ok=False,
                               content={"ok": False, "error": {"code": "bad_argument"}}),
                ],
            )
        assert first.kind == "tool_calls" and second.kind == "tool_calls"
        sent = mock_client.post.call_args.kwargs["json"]["messages"]
        tool_msgs = [m for m in sent if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
        assert json.loads(tool_msgs[0]["content"]) == {"ok": True, "cv": {}}
        # The assistant turn carrying the provider's own tool_calls precedes them.
        assistant_turns = [m for m in sent if m["role"] == "assistant"]
        assert [tc["id"] for tc in assistant_turns[-1]["tool_calls"]] == ["c1", "c2"]

    async def test_tools_still_attached_on_the_follow_up_request(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c1", "get_cv", "{}")))),
                MagicMock(status_code=200, text="y", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c2", "finalize", '{"change_log": "d"}')))),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            await backend.send_tool_results(
                handle, [ToolResult(call_id="c1", name="get_cv", ok=True, content={"ok": True})]
            )
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["tool_choice"] == "required"
        assert payload["tools"]

    async def test_text_reply_to_results_ends_tool_mode_without_a_sentinel_nudge(self):
        """The model answering prose instead of another batch is the loop's
        "non-tool_calls reply mid-loop" -> rung 3 path. It must not be nudged for a
        sentinel, and it must clear pending_tool_calls (the only place that happens)."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c1", "get_cv", "{}")))),
                MagicMock(status_code=200, text="y", json=MagicMock(
                    return_value=_completion_body(NO_SENTINEL_RAW))),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            reply = await backend.send_tool_results(
                handle, [ToolResult(call_id="c1", name="get_cv", ok=True, content={"ok": True})]
            )
        assert reply.kind == "final"
        assert handle.pending_tool_calls is None
        assert mock_client.post.await_count == 2  # no nudge replay on the second call
        # The assistant tool_calls turn + its tool results are now committed.
        assert [m["role"] for m in handle.messages] == ["user", "assistant", "tool", "assistant"]

    async def test_tools_rejected_mid_loop_is_a_tools_unsupported(self):
        """tool_loop.py wraps this exact call in `except ToolsUnsupported` (a routing
        gateway can resolve a different upstream per request, so a backend that
        accepted tools on round 1 can reject them on round 2). Confirms
        send_tool_results really can produce that exception, not just send_message."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=200, text="x", json=MagicMock(return_value=_tool_calls_body(
                    _tool_call_entry("c1", "get_cv", "{}")))),
                MagicMock(status_code=400, text="z", json=MagicMock(return_value={
                    "error": {"message": "no endpoints support tool use",
                              "type": "invalid_request_error"}})),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
            with pytest.raises(ToolsUnsupported) as exc_info:
                await backend.send_tool_results(
                    handle, [ToolResult(call_id="c1", name="get_cv", ok=True, content={"ok": True})]
                )
        assert isinstance(exc_info.value, _ToolsRejected)
        assert not isinstance(exc_info.value, AgentBackendUnavailable)
        assert mock_client.post.await_count == 2  # no in-backend retry-clean

    async def test_wrong_handle_type_raises(self):
        backend = MistralBackend()
        with pytest.raises(TypeError):
            await backend.send_tool_results(object(), [])  # type: ignore[arg-type]


class TestToolsRejectedDegrade:
    """A permanent 4xx with tools present is a TOOLS rejection, not a dead backend:
    tool_loop.py's native->prompt rung ladder owns the recovery, so this backend must
    neither retry clean in-process (as _CacheRejected/_ReasoningRejected do) nor tell
    BF-19 to advance the whole job."""

    async def test_permanent_4xx_with_tools_raises_tools_unsupported(self):
        mock_client = _make_mock_client(
            {"error": {"message": "no endpoints support tool use", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(ToolsUnsupported) as exc_info:
                await backend.send_message(handle, "go")
        assert isinstance(exc_info.value, _ToolsRejected)
        # NOT a BF-19 signal — that would advance the whole job to the next backend
        # over a tools-only degrade, exactly the loss the rung ladder prevents.
        assert not isinstance(exc_info.value, AgentBackendUnavailable)

    async def test_raised_without_an_in_backend_retry_clean(self):
        """Contrast with _CacheRejected/_ReasoningRejected, which DO retry once clean
        (see test_openrouter.py) — this one propagates after exactly one attempt."""
        mock_client = _make_mock_client(
            {"error": {"message": "tool use unsupported", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(prompt_caching=False)
            backend._reasoning = False
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(ToolsUnsupported):
                await backend.send_message(handle, "go")
        assert mock_client.post.await_count == 1

    async def test_tools_shed_first_when_tools_reasoning_and_caching_are_all_present(self):
        """Degrade precedence is tools -> reasoning -> caching -> fail. With all three
        on the wire, a 4xx names TOOLS, and neither enrichment flag is touched."""
        mock_client = _make_mock_client(
            {"error": {"message": "bad request", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(prompt_caching=True)  # prompt_cache_key present
            assert backend._reasoning is True               # reasoning_effort present
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(_ToolsRejected):
                await backend.send_message(handle, "go")
        assert backend._reasoning is True
        assert backend._prompt_caching is True
        assert mock_client.post.await_count == 1

    async def test_permanent_4xx_without_tools_is_unchanged_bf19_behavior(self):
        mock_client = _make_mock_client(
            {"error": {"message": "invalid model", "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend(prompt_caching=False)
            backend._reasoning = False
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend.start_session("sys", "hi")
        assert type(exc_info.value) is AgentBackendUnavailable
        assert not isinstance(exc_info.value, ToolsUnsupported)
        assert mock_client.post.await_count == 1

    async def test_429_with_tools_present_is_still_a_limit_signal(self):
        """Quota is account-scoped, not a tools problem — it must keep engaging BF-19."""
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(AgentLimitReached):
                await backend.send_message(handle, "go")


class TestToolSessionHandleState:
    async def test_restore_session_stores_the_vocabulary_on_the_handle(self):
        backend = MistralBackend()
        specs = _cv_specs()
        handle = await backend.restore_session("sys", [], None, tools=specs)
        assert handle.tools == specs
        assert handle.structured_enabled is False

    async def test_non_tool_session_leaves_the_field_none(self):
        backend = MistralBackend()
        handle = await backend.restore_session("sys", [], None)
        assert handle.tools is None
        assert handle.pending_tool_calls is None

    async def test_assistant_tool_calls_turn_is_parked_not_appended_until_results_return(self):
        """A turn ending on a terminal tool never sends results back, and an assistant
        tool_calls turn with no matching tool results is an invalid conversation."""
        mock_client = _make_mock_client(
            _tool_calls_body(_tool_call_entry("c1", "finalize", '{"change_log": "x"}'))
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            await backend.send_message(handle, "go")
        assert handle.messages == [{"role": "user", "content": "go"}]
        assert [tc["id"] for tc in handle.pending_tool_calls] == ["c1"]

    async def test_handle_untouched_when_the_call_fails(self):
        mock_client = _make_mock_client({}, status_code=429)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None, tools=_cv_specs())
            with pytest.raises(AgentLimitReached):
                await backend.send_message(handle, "go")
        assert handle.messages == []
        assert handle.pending_tool_calls is None


class TestToolModeGuardParity:
    """Two guards this base was missing that its three siblings (anthropic_api.py,
    gemini_api.py, opencode_zen.py) already carried."""

    async def test_start_session_text_reply_in_tool_mode_is_not_sentinel_nudged(self):
        """The start_session twin of TestToolReplyExtraction's send_message case.
        Without the tool branch this falls into _parse_structured_with_downgrade ->
        _parse_with_nudge, which re-prompts for a <<<FINAL>>> block the tool session
        was never given the contract for — and replays with tools stripped."""
        mock_client = _make_mock_client(_completion_body(NO_SENTINEL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "go", tools=_cv_specs())
        assert reply.kind == "final"
        assert mock_client.post.await_count == 1  # no nudge replay
        assert handle.structured_enabled is False

    async def test_send_tool_results_without_a_tool_session_raises_locally(self):
        """A prompt-rung handle (restored with no tools=) must fail loudly here, not
        POST role='tool' rows with an empty assistant tool_calls array and no tools
        field — that 400s and, since no tool fields were sent, classifies as a plain
        AgentBackendUnavailable, costing the job a BF-19 hop over a local bug."""
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle = await backend.restore_session("sys", [], None)
            with pytest.raises(ValueError, match="requires a native tool session"):
                await backend.send_tool_results(
                    handle,
                    [ToolResult(call_id="c1", name="get_cv", ok=True, content={"ok": True})],
                )
        assert mock_client.post.await_count == 0  # nothing ever went on the wire
