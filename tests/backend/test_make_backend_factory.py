import pytest

from jsa.agents.anthropic_api import AnthropicAPIBackend
from jsa.agents.claude_cli import ClaudeCliBackend
from jsa.agents.google_cli import GoogleCliBackend
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