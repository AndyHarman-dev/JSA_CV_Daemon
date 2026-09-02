"""Phase 1 of the prompt-caching plan: the `prompt_caching` kill switch.

This phase only wires the switch through (Settings -> CLI -> backend ctor ->
make_backend_factory) — no backend yet changes its request payload based on it
(that starts at Phase 2). So the only behavioral assertion here beyond "the flag
reaches the right places" is that the switch is currently a no-op on the wire: the
posted payload is byte-identical whether `prompt_caching` is True or False.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents._openai_compat import OpenAICompatBackend
from jsa.agents.gemini_api import GeminiBackend
from jsa.agents.mistral import MistralBackend
from jsa.agents.opencode_go import OpenCodeGoBackend
from jsa.agents.openrouter import OpenRouterBackend
from jsa.config import Settings
from jsa.server import make_backend_factory

FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"


def _completion_body(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _make_mock_client(json_body: dict) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value=json_body)
    mock_response.text = str(json_body)
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


class TestSettingsDefaultAndEnvOverride:
    def test_default_is_true(self, monkeypatch):
        monkeypatch.delenv("JSA_PROMPT_CACHING", raising=False)
        assert Settings().prompt_caching is True

    def test_env_override_false(self, monkeypatch):
        monkeypatch.setenv("JSA_PROMPT_CACHING", "false")
        assert Settings().prompt_caching is False

    def test_env_override_true(self, monkeypatch):
        monkeypatch.setenv("JSA_PROMPT_CACHING", "true")
        assert Settings().prompt_caching is True


class TestOpenAICompatBackendCtorKwarg:
    def test_default_is_true(self):
        assert OpenAICompatBackend.__init__ is not object.__init__  # sanity
        backend = MistralBackend()
        assert backend._prompt_caching is True

    def test_explicit_false(self):
        backend = MistralBackend(prompt_caching=False)
        assert backend._prompt_caching is False

    def test_explicit_true(self):
        backend = MistralBackend(prompt_caching=True)
        assert backend._prompt_caching is True


class TestEveryConcreteBackendAcceptsTheKwarg:
    """mistral/openrouter/gemini inherit __init__ unchanged; opencode-go overrides
    it but must forward the kwarg to super()."""

    def test_mistral(self):
        assert MistralBackend(prompt_caching=False)._prompt_caching is False

    def test_openrouter(self):
        assert OpenRouterBackend(prompt_caching=False)._prompt_caching is False

    def test_gemini(self):
        assert GeminiBackend(prompt_caching=False)._prompt_caching is False

    def test_opencode_go(self):
        backend = OpenCodeGoBackend(model="glm-5.3", prompt_caching=False)
        assert backend._prompt_caching is False

    def test_opencode_go_default_true(self):
        backend = OpenCodeGoBackend(model="glm-5.3")
        assert backend._prompt_caching is True


class TestFactoryForwardsSettingsFlag:
    """make_backend_factory must forward settings.prompt_caching to exactly the four
    new backends -- never to opencode-zen, claude-cli, google-cli, or anthropic
    (Phase 6), none of which accept the kwarg today."""

    @pytest.mark.parametrize("name", ["mistral", "openrouter", "gemini", "opencode-go"])
    def test_forwarded_true(self, name):
        settings = Settings(prompt_caching=True)
        backend = make_backend_factory(settings)(name)
        assert backend._prompt_caching is True

    @pytest.mark.parametrize("name", ["mistral", "openrouter", "gemini", "opencode-go"])
    def test_forwarded_false(self, name):
        settings = Settings(prompt_caching=False)
        backend = make_backend_factory(settings)(name)
        assert backend._prompt_caching is False

    def test_opencode_zen_untouched_by_the_flag(self):
        """opencode-zen is explicitly out of scope (undocumented caching API, keeps
        its own independent payload-builder copy per _openai_compat.py's module
        docstring) -- constructing it must not raise even with caching off."""
        settings = Settings(prompt_caching=False)
        backend = make_backend_factory(settings)("opencode-zen")
        assert not hasattr(backend, "_prompt_caching")

    def test_claude_cli_untouched_by_the_flag(self):
        settings = Settings(prompt_caching=False)
        backend = make_backend_factory(settings)("claude-cli")
        assert not hasattr(backend, "_prompt_caching")

    def test_google_cli_untouched_by_the_flag(self):
        settings = Settings(prompt_caching=False)
        backend = make_backend_factory(settings)("google-cli")
        assert not hasattr(backend, "_prompt_caching")

    def test_anthropic_untouched_by_the_flag(self):
        """Phase 6, not yet wired -- AnthropicAPIBackend doesn't accept the kwarg."""
        settings = Settings(prompt_caching=False)
        backend = make_backend_factory(settings)("anthropic")
        assert not hasattr(backend, "_prompt_caching")


class TestKillSwitchIsCurrentlyANoOpOnTheWire:
    """Phase 1 wired the switch through with no backend request-shape change yet.
    Mistral stopped being a no-op in Phase 2 (prompt_cache_key) -- see
    test_openai_compat.py::TestPromptCacheKey. OpenRouter stopped being a no-op in
    Phase 4 (cache_control breakpoint) -- see test_openrouter.py::
    TestPromptCacheControl. OpenCode-GO stopped being a no-op in Phase 5 (same
    cache_control breakpoint on both its protocols) -- see test_opencode_go.py::
    TestPromptCacheControlChat / TestPromptCacheControlMessages. Gemini is the one
    backend that is a PERMANENT no-op by design -- implicit caching needs no
    request-shape change at all (observability only, see gemini_api.py's module
    docstring) -- so it is the only backend left to cover here."""

    async def test_gemini_payload_identical_regardless_of_flag(self):
        payloads = {}
        for caching in (True, False):
            mock_client = _make_mock_client({"candidates": [{"content": {"parts": [{"text": FINAL_RAW}]}, "finishReason": "STOP"}]})
            with patch("httpx.AsyncClient", return_value=mock_client):
                backend = GeminiBackend(prompt_caching=caching)
                await backend.start_session("sys", "hi")
            payloads[caching] = mock_client.post.call_args.kwargs["json"]
        assert payloads[True] == payloads[False]
