"""Tests specific to jsa/agents/openrouter.py's OpenRouterBackend configuration.

The shared request/parse/retry/downgrade machinery is tested once, thoroughly, in
tests/backend/test_openai_compat.py. This file covers what's specific to
OpenRouter: the mandatory `provider.require_parameters` routing guard (silent
when missing, so only a test catches its removal) and namespaced model IDs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import _ToolsRejected
from jsa.agents.base import AgentBackendUnavailable, ToolsUnsupported
from jsa.agents.openrouter import OpenRouterBackend
from jsa.agents.tool_spec import tools_for
from jsa.schema.turn_models import json_schema_for
from jsa.db.models import Stage


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


def _completion_body(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


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


class TestOpenRouterBackendConfig:
    def test_name(self):
        assert OpenRouterBackend().name == "openrouter"

    def test_endpoint_url(self):
        assert OpenRouterBackend.endpoint_url == "https://openrouter.ai/api/v1/chat/completions"

    def test_env_vars(self):
        assert OpenRouterBackend.env_vars == ("OPENROUTER_API_KEY",)

    def test_default_model_is_namespaced(self):
        assert "/" in OpenRouterBackend()._model

    def test_default_model(self):
        assert OpenRouterBackend()._model == "nvidia/nemotron-3-nano-30b-a3b"


class TestRoutingGuard:
    async def test_provider_require_parameters_sent_on_every_request(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["provider"] == {"require_parameters": True}

    async def test_routing_guard_present_on_structured_requests_too(self):
        schema = json_schema_for(Stage.cv_adjust)
        body = _completion_body(
            '{"kind": "final", "question": null, "payload": '
            '{"contact": {"name": "Jane"}, "sections": []}}'
        )
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            await backend.start_session("sys", "hi", structured_schema=schema)
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["provider"] == {"require_parameters": True}
        assert "response_format" in payload


def _error_body(message: str = "no eligible provider") -> dict:
    return {"error": {"message": message, "type": "invalid_request_error"}}


class TestReasoningOptIn:
    async def test_reasoning_enabled_sent_on_every_request(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            await backend.start_session("sys", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["reasoning"] == {"enabled": True}

    async def test_reasoning_omitted_once_degraded(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            backend._reasoning = False
            await backend.start_session("sys", "hi")
        assert "reasoning" not in mock_client.post.call_args.kwargs["json"]


class TestPromptCacheControl:
    """Phase 4 of the prompt-caching plan: an explicit cache_control breakpoint on
    the system message, with a degrade-on-4xx path since sending it could plausibly
    route OpenRouter's request.provider.require_parameters guard to zero eligible
    providers (see CLAUDE.md -> "Prompt caching" -> OpenRouter's degrade-on-4xx)."""

    async def test_cache_control_sent_by_default(self):
        mock_client = _make_mock_client(_completion_body(FINAL_RAW))
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            await backend.start_session("sys prompt", "hi")
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["messages"][0] == {
            "role": "system",
            "content": [
                {"type": "text", "text": "sys prompt", "cache_control": {"type": "ephemeral"}}
            ],
        }

    async def test_prompt_caching_false_is_byte_identical_to_pre_phase4_payload(self):
        payloads = {}
        for caching in (True, False):
            mock_client = _make_mock_client(_completion_body(FINAL_RAW))
            with patch("httpx.AsyncClient", return_value=mock_client):
                backend = OpenRouterBackend(prompt_caching=caching)
                await backend.start_session("sys", "hi")
            payloads[caching] = mock_client.post.call_args.kwargs["json"]
        assert payloads[False]["messages"][0] == {"role": "system", "content": "sys"}
        assert payloads[True] != payloads[False]
        # The reasoning opt-in is a separate switch with its own degrade path and
        # is unaffected by the prompt-caching kill switch either way.
        assert payloads[False]["reasoning"] == {"enabled": True}

    async def test_permanent_4xx_degrades_reasoning_first_then_caching(self):
        """Both optional enrichments are present by default, so a permanent 4xx is
        shed one field at a time: reasoning first, prompt caching second. Each
        degrade costs exactly one extra attempt and is retried clean."""
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=400, json=MagicMock(return_value=_error_body()), text="x"),
                MagicMock(status_code=400, json=MagicMock(return_value=_error_body()), text="x2"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert backend._reasoning is False
        assert backend._prompt_caching is False
        assert mock_client.post.call_count == 3
        # Attempt 2 dropped `reasoning` but kept the cache_control breakpoint.
        second_payload = mock_client.post.call_args_list[1].kwargs["json"]
        assert "reasoning" not in second_payload
        assert isinstance(second_payload["messages"][0]["content"], list)
        # Attempt 3 dropped both.
        retried_payload = mock_client.post.call_args.kwargs["json"]
        assert "reasoning" not in retried_payload
        assert retried_payload["messages"][0] == {"role": "system", "content": "sys"}

    async def test_permanent_4xx_with_cache_fields_degrades_and_retries_clean(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=400, json=MagicMock(return_value=_error_body()), text="x"),
                MagicMock(status_code=200, json=MagicMock(return_value=_completion_body(FINAL_RAW)), text="y"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            backend._reasoning = False  # isolate the caching degrade from the reasoning one
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        assert backend._prompt_caching is False
        assert mock_client.post.call_count == 2
        retried_payload = mock_client.post.call_args.kwargs["json"]
        assert retried_payload["messages"][0] == {"role": "system", "content": "sys"}

    async def test_permanent_4xx_persists_after_clean_retry_also_fails_as_plain_unavailable(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=[
                MagicMock(status_code=400, json=MagicMock(return_value=_error_body()), text="x"),
                MagicMock(status_code=400, json=MagicMock(return_value=_error_body("bad model")), text="z"),
            ]
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            backend._reasoning = False
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend.start_session("sys", "hi")
        assert type(exc_info.value) is AgentBackendUnavailable
        assert backend._prompt_caching is False

    async def test_permanent_4xx_with_no_optional_fields_does_not_retry(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=MagicMock(status_code=400, json=MagicMock(return_value=_error_body("bad model")), text="x")
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend(prompt_caching=False)
            backend._reasoning = False
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.call_count == 1


class TestRoutingGuardWithNativeTools:
    """The mandatory `provider.require_parameters` guard (see TestRoutingGuard) now
    also filters on TOOL support, which can empty the eligible-provider pool and
    return a 4xx. That is EXPECTED, not a bug: it classifies as _ToolsRejected, so
    tool_loop.py drops to the prompt rung on the SAME backend instead of BF-19
    burning a whole backend hop over a tools-only rejection."""

    async def test_routing_guard_present_on_tool_requests_too(self):
        body = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "get_cv", "arguments": "{}"}}
                    ],
                }
            }]
        }
        mock_client = _make_mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            handle = await backend.restore_session(
                "sys", [], None, tools=tools_for(Stage.revising_cv)
            )
            reply = await backend.send_message(handle, "shorten it")
        assert reply.kind == "tool_calls"
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["provider"] == {"require_parameters": True}
        assert payload["tool_choice"] == "required"
        assert "response_format" not in payload

    async def test_4xx_with_tools_present_classifies_as_tools_rejected_not_unavailable(self):
        mock_client = _make_mock_client(
            {"error": {"message": "No endpoints found that support tool use",
                       "type": "invalid_request_error"}},
            status_code=400,
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend()
            handle = await backend.restore_session(
                "sys", [], None, tools=tools_for(Stage.revising_cv)
            )
            with pytest.raises(ToolsUnsupported) as exc_info:
                await backend.send_message(handle, "go")
        assert isinstance(exc_info.value, _ToolsRejected)
        # Must NOT engage BF-19 — that would advance the whole job to the next
        # configured backend over a rejection the rung ladder can absorb locally.
        assert not isinstance(exc_info.value, AgentBackendUnavailable)
        # And unlike _CacheRejected/_ReasoningRejected (see TestPromptCacheControl),
        # it is NOT retried once clean in-process: the rung ladder owns the retry.
        assert mock_client.post.call_count == 1
        assert backend._reasoning is True
        assert backend._prompt_caching is True
