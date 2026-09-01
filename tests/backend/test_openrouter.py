"""Tests specific to jsa/agents/openrouter.py's OpenRouterBackend configuration.

The shared request/parse/retry/downgrade machinery is tested once, thoroughly, in
tests/backend/test_openai_compat.py. This file covers what's specific to
OpenRouter: the mandatory `provider.require_parameters` routing guard (silent
when missing, so only a test catches its removal) and namespaced model IDs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
