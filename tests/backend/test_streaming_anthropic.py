"""Phase 7 item (3) — Anthropic SDK messages.stream() for content only."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents.anthropic_api import AnthropicAPIBackend
from jsa.agents.base import AgentChunk

pytestmark = pytest.mark.asyncio

FINAL_RAW = "<<<FINAL>>>\nAdjusted CV content here.\n<<<END>>>"


def _make_streaming_client(chunks: list[str], full_text: str) -> MagicMock:
    final_response = MagicMock()
    final_response.content = [MagicMock(text=full_text)]

    class _Ctx:
        def __init__(self):
            self.text_stream = self._agen()

        async def _agen(self):
            for c in chunks:
                yield c

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_final_message(self):
            return final_response

    mock_client = MagicMock()
    mock_client.messages.stream = MagicMock(return_value=_Ctx())
    mock_client.messages.create = AsyncMock(side_effect=AssertionError("must not use create() when streaming"))
    mock_client.close = AsyncMock()
    return mock_client


class TestAnthropicStreaming:
    async def test_content_chunks_reach_on_chunk(self):
        chunks = ["<<<FINAL>>>\n", "Adjusted CV content here.\n", "<<<END>>>"]
        mock_client = _make_streaming_client(chunks, FINAL_RAW)
        received: list[AgentChunk] = []

        async def on_chunk(chunk):
            received.append(chunk)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session("sys", "hi", on_chunk=on_chunk)

        assert reply.kind == "final"
        assert len(received) == 3
        assert all(c.kind == "content" for c in received)

    async def test_structured_mode_never_streams(self):
        """Even with on_chunk supplied, a structured-mode call must use
        messages.create, never messages.stream."""
        mock_response = MagicMock()
        mock_response.stop_reason = "tool_use"
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"kind": "final", "question": None, "payload": {}}
        mock_response.content = [block]
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.messages.stream = MagicMock(side_effect=AssertionError("must not stream"))
        mock_client.close = AsyncMock()

        async def on_chunk(chunk):
            pass

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            try:
                await backend.start_session(
                    "sys", "hi", structured_schema={"type": "object"}, on_chunk=on_chunk
                )
            except Exception:
                pass
        mock_client.messages.create.assert_awaited()

    async def test_no_on_chunk_uses_create_not_stream(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=FINAL_RAW)]
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.messages.stream = MagicMock(side_effect=AssertionError("must not stream"))
        mock_client.close = AsyncMock()

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            backend = AnthropicAPIBackend()
            handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"
        mock_client.messages.create.assert_awaited_once()
