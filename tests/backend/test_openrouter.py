"""Tests specific to jsa/agents/openrouter.py's OpenRouterBackend configuration.

The shared request/parse/retry/downgrade machinery is tested once, thoroughly, in
tests/backend/test_openai_compat.py. This file covers what's specific to
OpenRouter: the mandatory `provider.require_parameters` routing guard (silent
when missing, so only a test catches its removal) and namespaced model IDs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents.base import AgentBackendUnavailable
from jsa.agents.openrouter import OpenRouterBackend
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
            with pytest.raises(AgentBackendUnavailable) as exc_info:
                await backend.start_session("sys", "hi")
        assert type(exc_info.value) is AgentBackendUnavailable
        assert backend._prompt_caching is False

    async def test_permanent_4xx_without_prompt_caching_does_not_retry(self):
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=MagicMock(status_code=400, json=MagicMock(return_value=_error_body("bad model")), text="x")
        )
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = OpenRouterBackend(prompt_caching=False)
            with pytest.raises(AgentBackendUnavailable):
                await backend.start_session("sys", "hi")
        assert mock_client.post.call_count == 1
