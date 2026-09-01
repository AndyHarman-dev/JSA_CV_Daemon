"""Live integration test for MistralBackend against the real API.

Hits https://api.mistral.ai/v1/chat/completions using MISTRAL_API_KEY from the
repo-root .env file. Skipped by default (`pytest -m "not integration"`); run
explicitly with `pytest tests/backend/integration/test_mistral_live.py -v -m integration`.

Per CLAUDE.md's Phase 0 note: a silent skip is not a pass — this file, and every
other file in this directory, must actually be run with a real exported key by the
user before the multi-backend-model-select plan's Phase 7 can call it verified.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.mistral import MistralBackend

pytestmark = pytest.mark.integration


def _load_dotenv_key(name: str) -> str | None:
    """Minimal .env reader — no python-dotenv dependency in this project.

    Looks for `name=value` in the repo-root .env, without overriding a value
    already present in the real environment. Mirrors test_opencode_zen_live.py's
    helper of the same name (duplicated rather than imported, matching that
    file's own convention of not sharing test helpers across integration
    test modules).
    """
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
def mistral_api_key() -> str:
    key = _load_dotenv_key("MISTRAL_API_KEY")
    if not key:
        pytest.skip("MISTRAL_API_KEY not set in environment or .env")
    os.environ["MISTRAL_API_KEY"] = key
    return key


class TestLiveStartSession:
    async def test_start_session_returns_final_reply(self, mistral_api_key):
        backend = MistralBackend(timeout=60.0)
        handle, reply = await backend.start_session(
            "You are a terse assistant that always replies using the sentinel "
            "protocol below and nothing else.\n\n"
            "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
            "Please reply now.",
        )
        assert reply.kind == "final"
        assert "OK" in reply.content

    async def test_bad_model_raises_runtime_error(self, mistral_api_key):
        from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached

        backend = MistralBackend(model="not-a-real-mistral-model", timeout=30.0)
        with pytest.raises((AgentBackendUnavailable, AgentLimitReached)):
            await backend.start_session("sys", "hello")
