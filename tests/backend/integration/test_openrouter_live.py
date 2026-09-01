"""Live integration test for OpenRouterBackend against the real API.

Hits https://openrouter.ai/api/v1/chat/completions using OPENROUTER_API_KEY from
the repo-root .env file. Skipped by default (`pytest -m "not integration"`); run
explicitly with `pytest tests/backend/integration/test_openrouter_live.py -v -m integration`.

This is also the live check for the mandatory `provider.require_parameters`
routing guard (jsa/agents/openrouter.py): if OpenRouter ever routes this default
model to an upstream endpoint that silently ignores `response_format` despite the
guard, the structured-mode test below would surface it as an unparseable-JSON
downgrade instead of a clean structured reply.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.openrouter import OpenRouterBackend

pytestmark = pytest.mark.integration


def _load_dotenv_key(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


@pytest.fixture(scope="module")
def openrouter_api_key() -> str:
    key = _load_dotenv_key("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY not set in environment or .env")
    os.environ["OPENROUTER_API_KEY"] = key
    return key


class TestLiveStartSession:
    async def test_start_session_returns_final_reply(self, openrouter_api_key):
        backend = OpenRouterBackend(timeout=60.0)
        handle, reply = await backend.start_session(
            "You are a terse assistant that always replies using the sentinel "
            "protocol below and nothing else.\n\n"
            "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
            "Please reply now.",
        )
        assert reply.kind == "final"
        assert "OK" in reply.content

    async def test_bad_model_raises_runtime_error(self, openrouter_api_key):
        from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached

        backend = OpenRouterBackend(model="not-a-real/openrouter-model", timeout=30.0)
        with pytest.raises((AgentBackendUnavailable, AgentLimitReached)):
            await backend.start_session("sys", "hello")
