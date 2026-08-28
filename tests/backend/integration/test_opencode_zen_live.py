"""Live integration test for OpenCodeZenBackend against the real API.

Hits https://opencode.ai/zen/v1/chat/completions using OPENCODE_API_KEY from the
repo-root .env file. Skipped by default (`pytest -m "not integration"`); run
explicitly with `pytest tests/backend/integration/test_opencode_zen_live.py -v -m integration`.

The free `nemotron-3-ultra-free` model is observably flaky (intermittent 502
"Upstream error from Nvidia: Service temporarily overloaded" even on HTTP 200 —
see jsa/agents/opencode_zen.py's body-level error check), so the happy-path test
retries a few times before failing.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.opencode_zen import OpenCodeZenBackend

pytestmark = pytest.mark.integration


def _load_dotenv_key(name: str) -> str | None:
    """Minimal .env reader — no python-dotenv dependency in this project.

    Looks for `name=value` in the repo-root .env, without overriding a value
    already present in the real environment.
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
def opencode_api_key() -> str:
    key = _load_dotenv_key("OPENCODE_API_KEY")
    if not key:
        pytest.skip("OPENCODE_API_KEY not set in environment or .env")
    os.environ["OPENCODE_API_KEY"] = key
    return key


class TestLiveStartSession:
    async def test_start_session_returns_final_reply(self, opencode_api_key):
        """The free model is flaky (intermittent upstream 502s); retry a few times."""
        backend = OpenCodeZenBackend(timeout=60.0)
        last_exc: Exception | None = None
        for _ in range(5):
            try:
                handle, reply = await backend.start_session(
                    "You are a terse assistant that always replies using the sentinel "
                    "protocol below and nothing else.\n\n"
                    "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
                    "Please reply now.",
                )
            except Exception as exc:  # noqa: BLE001 — transient upstream flakiness, retry
                last_exc = exc
                continue
            assert reply.kind == "final"
            assert "OK" in reply.content
            return
        pytest.fail(f"opencode-zen live call failed after 5 attempts: {last_exc}")

    async def test_bad_model_raises_runtime_error(self, opencode_api_key):
        """A model id the API rejects outright (no retry needed — deterministic)."""
        from jsa.agents.base import AgentLimitReached

        backend = OpenCodeZenBackend(model="opencode/nemotron-3-ultra-free", timeout=30.0)
        with pytest.raises((RuntimeError, AgentLimitReached)):
            await backend.start_session("sys", "hello")
