import pytest

from jsa.agents.anthropic_api import AnthropicAPIBackend
from jsa.agents.claude_cli import ClaudeCliBackend
from jsa.agents.gemini_api import GeminiBackend
from jsa.agents.google_cli import GoogleCliBackend
from jsa.agents.mistral import MistralBackend
from jsa.agents.opencode_go import OpenCodeGoBackend
from jsa.agents.openrouter import OpenRouterBackend
from jsa.config import Settings
from jsa.server import make_backend_factory

def test_make_backend_factory_anthropic():
    settings = Settings()
    backend = make_backend_factory(settings)

    agent = backend("anthropic")
    assert isinstance(agent, AnthropicAPIBackend)

def test_make_backend_factory_claude_cli():
    settings = Settings()
    backend = make_backend_factory(settings)

    agent = backend("claude-cli")
    assert isinstance(agent, ClaudeCliBackend)


def test_make_backend_factory_google_cli():
    settings = Settings()
    backend = make_backend_factory(settings)

    agent = backend("google-cli")
    assert isinstance(agent, GoogleCliBackend)


def test_make_backend_factory_not_a_backend():
    settings = Settings()
    backend = make_backend_factory(settings)

    with pytest.raises(KeyError):
        backend("harry-potter")


class TestNewBackendsFactory:
    """Phase 4: the four new backends get explicit _backend_factory branches, each
    resolving through the same _model_for precedence as the existing four."""

    def test_make_backend_factory_mistral(self):
        settings = Settings()
        backend = make_backend_factory(settings)

        agent = backend("mistral")
        assert isinstance(agent, MistralBackend)
        assert agent._model == settings.mistral_model

    def test_make_backend_factory_openrouter(self):
        settings = Settings()
        backend = make_backend_factory(settings)

        agent = backend("openrouter")
        assert isinstance(agent, OpenRouterBackend)
        assert agent._model == settings.openrouter_model

    def test_make_backend_factory_gemini(self):
        settings = Settings()
        backend = make_backend_factory(settings)

        agent = backend("gemini")
        assert isinstance(agent, GeminiBackend)
        assert agent._model == settings.gemini_model

    def test_make_backend_factory_opencode_go(self):
        settings = Settings()
        backend = make_backend_factory(settings)

        agent = backend("opencode-go")
        assert isinstance(agent, OpenCodeGoBackend)
        assert agent._model == settings.opencode_go_model

    def test_runtime_selection_applies_to_new_backends(self):
        settings = Settings(backend_models={"mistral": "some-other-model"})
        backend = make_backend_factory(settings)

        agent = backend("mistral")
        assert agent._model == "some-other-model"

    def test_explicit_override_wins_for_new_backends(self):
        settings = Settings(backend_models={"gemini": "some-other-model"})
        backend = make_backend_factory(settings, model_override="pinned-model")

        agent = backend("gemini")
        assert agent._model == "pinned-model"


class TestSettingsDefaultsMatchBackendClassDefaults:
    """config.py hardcodes each new backend's default model as a literal (matching the
    existing precedent for `model`/`opencode_zen_model`) rather than importing the
    class -- this guards the two literals from silently drifting apart."""

    def test_mistral_default_matches_class(self):
        assert Settings().mistral_model == MistralBackend.default_model

    def test_openrouter_default_matches_class(self):
        assert Settings().openrouter_model == OpenRouterBackend.default_model

    def test_gemini_default_matches_class(self):
        assert Settings().gemini_model == GeminiBackend.default_model

    def test_opencode_go_default_matches_class(self):
        assert Settings().opencode_go_model == OpenCodeGoBackend.default_model


class TestModelPrecedence:
    """explicit override > settings.backend_models[name] > flat per-backend default."""

    def test_backend_models_selection_used_when_no_override(self):
        settings = Settings(backend_models={"anthropic": "claude-opus-5"})
        backend = make_backend_factory(settings)

        agent = backend("anthropic")
        assert agent._model == "claude-opus-5"

    def test_explicit_override_wins_over_backend_models_selection(self):
        settings = Settings(backend_models={"anthropic": "claude-opus-5"})
        backend = make_backend_factory(settings, model_override="claude-haiku-4-5")

        agent = backend("anthropic")
        assert agent._model == "claude-haiku-4-5"

    def test_flat_default_used_when_no_selection_and_no_override(self):
        settings = Settings()
        backend = make_backend_factory(settings)

        agent = backend("anthropic")
        assert agent._model == settings.model

    def test_opencode_zen_selection_never_falls_back_to_shared_model(self):
        settings = Settings(model="claude-haiku-4-5", backend_models={"opencode-zen": "mimo-v2.5-free"})
        backend = make_backend_factory(settings)

        agent = backend("opencode-zen")
        assert agent._model == "mimo-v2.5-free"

    def test_claude_cli_selection_is_independent_of_anthropic_selection(self):
        settings = Settings(backend_models={"anthropic": "claude-opus-5"})
        backend = make_backend_factory(settings)

        agent = backend("claude-cli")
        assert agent._model == settings.model

    def test_google_cli_ignores_backend_models_selection(self):
        settings = Settings(backend_models={"google-cli": "irrelevant"})
        backend = make_backend_factory(settings)

        agent = backend("google-cli")
        assert not hasattr(agent, "_model")

    def test_runtime_mutation_of_backend_models_is_seen_on_next_call(self):
        """The factory closure re-reads settings.backend_models on every call, so a
        PUT /api/backend-models mutation reaches the very next dispatch with no restart."""
        settings = Settings()
        backend = make_backend_factory(settings)

        assert backend("anthropic")._model == settings.model

        settings.backend_models["anthropic"] = "claude-sonnet-5"
        assert backend("anthropic")._model == "claude-sonnet-5"