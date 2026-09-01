"""Live integration test for GeminiBackend against the real generateContent API.

Hits https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
using GEMINI_API_KEY (falling back to GOOGLE_API_KEY) from the repo-root .env file.
Skipped by default (`pytest -m "not integration"`); run explicitly with
`pytest tests/backend/integration/test_gemini_live.py -v -m integration`.

This is also the live check for two things the mocked test suite cannot confirm:
- the `x-goog-api-key` header form (the plan's locked decision — the Phase-0 probe
  script only exercised the `?key=` query-string form; see the Phase 3 Change Log's
  "Auth header form is plan-specified, not live-probe-confirmed" note).
- the inline_defs()-flattened structured-output schema actually being accepted by
  generationConfig.responseSchema end-to-end (not just the Phase-0 probe's one-off
  curl, and not just the mocked shape assertion in test_gemini_api.py).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.gemini_api import GeminiBackend
from jsa.db.models import Stage
from jsa.schema.turn_models import json_schema_for

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
def gemini_api_key() -> str:
    key = _load_dotenv_key("GEMINI_API_KEY") or _load_dotenv_key("GOOGLE_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY / GOOGLE_API_KEY not set in environment or .env")
    os.environ["GEMINI_API_KEY"] = key
    return key


class TestLiveStartSession:
    async def test_start_session_returns_final_reply(self, gemini_api_key):
        backend = GeminiBackend(timeout=60.0)
        handle, reply = await backend.start_session(
            "You are a terse assistant that always replies using the sentinel "
            "protocol below and nothing else.\n\n"
            "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
            "Please reply now.",
        )
        assert reply.kind == "final"
        assert "OK" in reply.content

    async def test_structured_final_reply_accepted(self, gemini_api_key):
        """The real generateContent endpoint accepts the inline_defs()-flattened
        cv_adjust schema and returns a JSON kind='final' reply, not a rejection."""
        schema = json_schema_for(Stage.cv_adjust)
        backend = GeminiBackend(timeout=60.0)
        handle, reply = await backend.start_session(
            "You are a CV-tailoring assistant. Reply with a minimal valid JSON "
            "object matching the schema you were given: kind='final', a null "
            "question, and any minimally valid payload.",
            "Please reply now.",
            structured_schema=schema,
        )
        assert reply.kind in ("final", "needs_input")
        # A schema rejection would have downgraded this session to sentinel mode.
        assert handle.structured_enabled is True

    async def test_bad_model_raises_runtime_error(self, gemini_api_key):
        from jsa.agents.base import AgentBackendUnavailable, AgentLimitReached

        backend = GeminiBackend(model="not-a-real-gemini-model", timeout=30.0)
        with pytest.raises((AgentBackendUnavailable, AgentLimitReached)):
            await backend.start_session("sys", "hello")
