"""Tests specific to jsa/agents/mistral.py's MistralBackend configuration.

The shared request/parse/retry/downgrade machinery is tested once, thoroughly, in
tests/backend/test_openai_compat.py (exercised via this same class, since Mistral
has no overrides of its own). This file only covers what's specific to Mistral.
"""

from __future__ import annotations

from jsa.agents.mistral import MistralBackend


class TestMistralBackendConfig:
    def test_name(self):
        assert MistralBackend().name == "mistral"

    def test_endpoint_url(self):
        assert MistralBackend.endpoint_url == "https://api.mistral.ai/v1/chat/completions"

    def test_env_vars(self):
        assert MistralBackend.env_vars == ("MISTRAL_API_KEY",)

    def test_default_model(self):
        assert MistralBackend().default_model == "mistral-small-2603"
        assert MistralBackend()._model == "mistral-small-2603"

    def test_model_override(self):
        assert MistralBackend(model="mistral-large-2512")._model == "mistral-large-2512"

    def test_supports_structured_output(self):
        assert MistralBackend.supports_structured_output is True

    def test_default_timeout(self):
        assert MistralBackend()._timeout == 180.0
