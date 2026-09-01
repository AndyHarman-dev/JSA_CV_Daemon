"""Live integration test for OpenCodeGoBackend against the real API.

Hits https://opencode.ai/zen/go/v1 using OPENCODE_GO_API_KEY (falling back to
OPENCODE_API_KEY) from the repo-root .env file. Skipped by default
(`pytest -m "not integration"`); run explicitly with
`pytest tests/backend/integration/test_opencode_go_live.py -v -m integration`.

Covers BOTH wire protocols this backend dispatches between — the Phase-0 probe
(#5) only confirmed the `/messages` auth header shape with a one-off curl, never
through OpenCodeGoBackend itself, and no mocked test can confirm the live
`/chat/completions` endpoint actually accepts this project's payload shape.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.opencode_go import OpenCodeGoBackend

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
def opencode_go_api_key() -> str:
    key = _load_dotenv_key("OPENCODE_GO_API_KEY") or _load_dotenv_key("OPENCODE_API_KEY")
    if not key:
        pytest.skip("OPENCODE_GO_API_KEY / OPENCODE_API_KEY not set in environment or .env")
    os.environ["OPENCODE_GO_API_KEY"] = key
    return key


class TestLiveChatProtocol:
    async def test_default_model_chat_protocol_returns_final_reply(self, opencode_go_api_key):
        """Default model (glm-5.3) is /chat/completions."""
        backend = OpenCodeGoBackend(timeout=60.0)
        assert backend._protocol == "chat"
        handle, reply = await backend.start_session(
            "You are a terse assistant that always replies using the sentinel "
            "protocol below and nothing else.\n\n"
            "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
            "Please reply now.",
        )
        assert reply.kind == "final"
        assert "OK" in reply.content


class TestLiveMessagesProtocol:
    async def test_messages_model_returns_final_reply(self, opencode_go_api_key):
        """qwen3.8-max is confirmed /messages (Anthropic-shape, sentinel-only)."""
        backend = OpenCodeGoBackend(model="qwen3.8-max", timeout=60.0)
        assert backend._protocol == "messages"
        assert backend.supports_structured_output is False
        handle, reply = await backend.start_session(
            "You are a terse assistant that always replies using the sentinel "
            "protocol below and nothing else.\n\n"
            "Reply with exactly:\n<<<FINAL>>>\nOK\n<<<END>>>",
            "Please reply now.",
        )
        assert reply.kind == "final"
        assert "OK" in reply.content
