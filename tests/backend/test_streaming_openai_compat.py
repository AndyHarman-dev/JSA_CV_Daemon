"""Phase 7 item (1)/(2) — SSE streaming for OpenAICompatBackend (mistral, openrouter,
opencode-go's /chat protocol all share this base)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jsa.agents.base import AgentChunk
from jsa.agents.mistral import MistralBackend

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    async def _instant_sleep(_seconds):
        return None
    monkeypatch.setattr("jsa.agents._openai_compat.asyncio.sleep", _instant_sleep)


def _sse_lines(events: list[dict]) -> list[str]:
    import json
    return [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]


def _make_mock_stream_client(lines: list[str], status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code

    async def _aiter_lines():
        for line in lines:
            yield line

    mock_response.aiter_lines = _aiter_lines
    mock_response.aread = AsyncMock(return_value=b"")

    class _StreamCtx:
        async def __aenter__(self):
            return mock_response

        async def __aexit__(self, *a):
            return False

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=_StreamCtx())
    mock_client.aclose = AsyncMock()
    return mock_client


class TestOpenAICompatStreaming:
    async def test_content_chunks_reach_on_chunk_and_reply_is_correct(self):
        lines = _sse_lines(
            [
                {"choices": [{"delta": {"content": "<<<FINAL>>>\n"}}]},
                {"choices": [{"delta": {"content": "Adjusted CV.\n"}}]},
                {"choices": [{"delta": {"content": "<<<END>>>"}}]},
            ]
        )
        mock_client = _make_mock_stream_client(lines)
        received: list[AgentChunk] = []

        async def on_chunk(chunk: AgentChunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi", on_chunk=on_chunk)

        assert reply.kind == "final"
        assert "Adjusted CV." in reply.content
        assert len(received) == 3
        assert all(c.kind == "content" for c in received)
        assert "".join(c.text for c in received) == "<<<FINAL>>>\nAdjusted CV.\n<<<END>>>"

    async def test_reasoning_delta_forwarded_when_present(self):
        lines = _sse_lines(
            [
                {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
                {"choices": [{"delta": {"content": "<<<FINAL>>>\nx\n<<<END>>>"}}]},
            ]
        )
        mock_client = _make_mock_stream_client(lines)
        received: list[AgentChunk] = []

        async def on_chunk(chunk: AgentChunk) -> None:
            received.append(chunk)

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            await backend.start_session("sys", "hi", on_chunk=on_chunk)

        assert any(c.kind == "reasoning" and c.text == "thinking..." for c in received)

    async def test_no_on_chunk_uses_non_streaming_path(self):
        """When on_chunk is None, `stream: true` must never be sent — client.post,
        not client.stream, is used (byte-identical to pre-streaming behavior)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={"choices": [{"message": {"content": "<<<FINAL>>>\nx\n<<<END>>>"}}]}
        )
        mock_response.text = "..."
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.stream = MagicMock(side_effect=AssertionError("must not stream"))
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session("sys", "hi")

        assert reply.kind == "final"
        mock_client.post.assert_awaited_once()
        posted_payload = mock_client.post.call_args.kwargs["json"]
        assert "stream" not in posted_payload

    async def test_structured_schema_never_streams(self):
        """Structured mode must never attempt SSE, even if on_chunk is supplied."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json = MagicMock(
            return_value={
                "choices": [
                    {"message": {"content": '{"kind":"final","question":null,"payload":{}}'}}
                ]
            }
        )
        mock_response.text = "..."
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.stream = MagicMock(side_effect=AssertionError("must not stream"))
        mock_client.aclose = AsyncMock()

        async def on_chunk(chunk):
            pass

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            try:
                await backend.start_session(
                    "sys", "hi", structured_schema={"type": "object"}, on_chunk=on_chunk
                )
            except Exception:
                pass  # only the streaming-vs-non-streaming dispatch is under test here
        mock_client.post.assert_awaited()


class TestOnRetryHook:
    async def test_nudge_calls_on_retry_before_replaying(self):
        """A sentinel-less first reply triggers _parse_with_nudge's replay —
        on_retry must be awaited before that replay, so the caller (stages.py's
        ChunkAccumulator) can mark the first attempt's streamed buffer
        superseded before the second attempt starts appending."""
        first_call_lines = _sse_lines(
            [{"choices": [{"delta": {"content": "no sentinel here at all"}}]}]
        )
        second_call_lines = _sse_lines(
            [{"choices": [{"delta": {"content": "<<<FINAL>>>\nok\n<<<END>>>"}}]}]
        )

        responses = [first_call_lines, second_call_lines]

        def _next_stream_ctx(*a, **kw):
            lines = responses.pop(0)
            mock_response = MagicMock()
            mock_response.status_code = 200

            async def _aiter_lines():
                for line in lines:
                    yield line

            mock_response.aiter_lines = _aiter_lines
            mock_response.aread = AsyncMock(return_value=b"")

            class _Ctx:
                async def __aenter__(self):
                    return mock_response

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

        mock_client = MagicMock()
        mock_client.stream = MagicMock(side_effect=_next_stream_ctx)
        mock_client.aclose = AsyncMock()

        retry_calls = {"n": 0}

        async def on_retry():
            retry_calls["n"] += 1

        async def on_chunk(chunk):
            pass

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session(
                "sys", "hi", on_chunk=on_chunk, on_retry=on_retry
            )

        assert reply.kind == "final"
        assert retry_calls["n"] == 1
        assert mock_client.stream.call_count == 2

    async def test_transient_http_retry_calls_on_retry(self):
        """retry_transient's own in-backend transient-HTTP retry loop must also
        call on_retry before each retried attempt (attempt 1 -> transient 502,
        attempt 2 -> success)."""
        attempts = {"n": 0}

        def _next_stream_ctx(*a, **kw):
            attempts["n"] += 1
            mock_response = MagicMock()
            if attempts["n"] == 1:
                mock_response.status_code = 502

                async def _aiter_lines_err():
                    return
                    yield  # pragma: no cover - empty generator

                mock_response.aiter_lines = _aiter_lines_err
                mock_response.aread = AsyncMock(return_value=b"gateway error")
            else:
                mock_response.status_code = 200
                lines = _sse_lines([{"choices": [{"delta": {"content": "<<<FINAL>>>\nok\n<<<END>>>"}}]}])

                async def _aiter_lines_ok():
                    for line in lines:
                        yield line

                mock_response.aiter_lines = _aiter_lines_ok
                mock_response.aread = AsyncMock(return_value=b"")

            class _Ctx:
                async def __aenter__(self):
                    return mock_response

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

        mock_client = MagicMock()
        mock_client.stream = MagicMock(side_effect=_next_stream_ctx)
        mock_client.aclose = AsyncMock()

        retry_calls = {"n": 0}

        async def on_retry():
            retry_calls["n"] += 1

        async def on_chunk(chunk):
            pass

        with patch("httpx.AsyncClient", return_value=mock_client):
            backend = MistralBackend()
            handle, reply = await backend.start_session(
                "sys", "hi", on_chunk=on_chunk, on_retry=on_retry
            )

        assert reply.kind == "final"
        assert retry_calls["n"] == 1
        assert attempts["n"] == 2
