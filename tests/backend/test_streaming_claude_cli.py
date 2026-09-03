"""Phase 7 item (7) — ClaudeCliBackend's NDJSON stream-json whitelist parser.

Monkeypatches jsa.agents.claude_cli.run_killable_streaming (rather than spawning a
real `claude`) to feed a scripted NDJSON event sequence through the SAME on_line
callback _run_streaming builds — this exercises the whitelist parsing logic
directly. Real-subprocess killability is covered separately in
test_streaming_subprocess.py.
"""

from __future__ import annotations

import json

import pytest

from jsa.agents._subprocess import KillableResult
from jsa.agents.base import AgentChunk, AgentLimitReached
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeCliError, ClaudeSessionExpiredError

pytestmark = pytest.mark.asyncio


def _ndjson(events: list[dict]) -> bytes:
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _patch_run_killable_streaming(monkeypatch, events: list[dict], returncode: int = 0, stderr: bytes = b""):
    """Replay `events` through the on_line callback exactly as the real
    run_killable_streaming would, one at a time, then return a KillableResult."""

    async def fake_run_killable_streaming(cmd, *, timeout, cwd=None, label="subprocess", on_line=None):
        stdout_parts = []
        for event in events:
            line = (json.dumps(event) + "\n").encode()
            stdout_parts.append(line)
            if on_line is not None:
                await on_line(line)
        return KillableResult(returncode, b"".join(stdout_parts), stderr)

    monkeypatch.setattr(
        "jsa.agents.claude_cli.run_killable_streaming", fake_run_killable_streaming
    )


class TestWhitelistParsing:
    async def test_content_and_reasoning_deltas_forwarded(self, monkeypatch):
        events = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "let me think"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "<<<FINAL>>>\n"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi\n<<<END>>>"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "<<<FINAL>>>\nhi\n<<<END>>>"}]}},
            {"type": "result", "subtype": "success"},
        ]
        _patch_run_killable_streaming(monkeypatch, events)
        received: list[AgentChunk] = []

        async def on_chunk(chunk):
            received.append(chunk)

        backend = ClaudeCliBackend(timeout=5.0)
        handle, reply = await backend.start_session("sys", "hi", on_chunk=on_chunk)
        assert reply.kind == "final"
        assert reply.content.strip() == "hi"
        assert any(c.kind == "reasoning" for c in received)
        assert any(c.kind == "content" for c in received)

    async def test_hook_system_events_never_forwarded(self, monkeypatch):
        """system events (hook_started/hook_response) carry arbitrary output text
        and must be ignored entirely — never turned into a chunk."""
        events = [
            {"type": "system", "subtype": "hook_started", "output": "SECRET HOOK OUTPUT"},
            {"type": "system", "subtype": "hook_response", "output": "MORE SECRET OUTPUT"},
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "<<<FINAL>>>\nok\n<<<END>>>"}}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "<<<FINAL>>>\nok\n<<<END>>>"}]}},
        ]
        _patch_run_killable_streaming(monkeypatch, events)
        received: list[AgentChunk] = []

        async def on_chunk(chunk):
            received.append(chunk)

        backend = ClaudeCliBackend(timeout=5.0)
        handle, reply = await backend.start_session("sys", "hi", on_chunk=on_chunk)
        assert all("SECRET" not in c.text for c in received)
        assert reply.kind == "final"

    async def test_rate_limit_event_raises_agent_limit_reached(self, monkeypatch):
        events = [
            {"type": "rate_limit_event", "message": "usage limit reached"},
        ]
        _patch_run_killable_streaming(monkeypatch, events)

        async def on_chunk(chunk):
            pass

        backend = ClaudeCliBackend(timeout=5.0)
        with pytest.raises(AgentLimitReached):
            await backend.start_session("sys", "hi", on_chunk=on_chunk)

    async def test_non_zero_exit_with_session_expired_stderr(self, monkeypatch):
        _patch_run_killable_streaming(
            monkeypatch, [], returncode=1, stderr=b"No conversation found for session xyz"
        )

        async def on_chunk(chunk):
            pass

        backend = ClaudeCliBackend(timeout=5.0)
        with pytest.raises(ClaudeSessionExpiredError):
            await backend.start_session("sys", "hi", on_chunk=on_chunk)

    async def test_non_zero_exit_generic_error(self, monkeypatch):
        _patch_run_killable_streaming(monkeypatch, [], returncode=1, stderr=b"boom")

        async def on_chunk(chunk):
            pass

        backend = ClaudeCliBackend(timeout=5.0)
        with pytest.raises(ClaudeCliError):
            await backend.start_session("sys", "hi", on_chunk=on_chunk)

    async def test_no_on_chunk_uses_plain_run_not_streaming(self, monkeypatch):
        """Without on_chunk, run_killable_streaming must never be invoked at all —
        confirms the conditional-kwarg / non-streaming path is byte-identical."""
        async def fail_if_called(*a, **kw):
            raise AssertionError("must not stream")

        monkeypatch.setattr("jsa.agents.claude_cli.run_killable_streaming", fail_if_called)

        async def fake_run_killable(cmd, *, timeout, cwd=None, label="subprocess"):
            return KillableResult(0, b"<<<FINAL>>>\nok\n<<<END>>>", b"")

        monkeypatch.setattr("jsa.agents.claude_cli.run_killable", fake_run_killable)

        backend = ClaudeCliBackend(timeout=5.0)
        handle, reply = await backend.start_session("sys", "hi")
        assert reply.kind == "final"


class TestOnRetryOnNudge:
    async def test_on_retry_called_before_nudge_replay(self, monkeypatch):
        """A sentinel-less first reply triggers _parse_with_nudge's replay via
        --resume — on_retry must be awaited before that replay."""
        calls = {"n": 0}

        async def fake_run_killable_streaming(cmd, *, timeout, cwd=None, label="subprocess", on_line=None):
            calls["n"] += 1
            if calls["n"] == 1:
                events = [
                    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "no sentinel at all"}}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "no sentinel at all"}]}},
                ]
            else:
                events = [
                    {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "<<<FINAL>>>\nok\n<<<END>>>"}}},
                    {"type": "assistant", "message": {"content": [{"type": "text", "text": "<<<FINAL>>>\nok\n<<<END>>>"}]}},
                ]
            stdout_parts = []
            for event in events:
                line = (json.dumps(event) + "\n").encode()
                stdout_parts.append(line)
                if on_line is not None:
                    await on_line(line)
            return KillableResult(0, b"".join(stdout_parts), b"")

        monkeypatch.setattr(
            "jsa.agents.claude_cli.run_killable_streaming", fake_run_killable_streaming
        )

        retry_calls = {"n": 0}

        async def on_retry():
            retry_calls["n"] += 1

        async def on_chunk(chunk):
            pass

        backend = ClaudeCliBackend(timeout=5.0)
        handle, reply = await backend.start_session(
            "sys", "hi", on_chunk=on_chunk, on_retry=on_retry
        )
        assert reply.kind == "final"
        assert retry_calls["n"] == 1
        assert calls["n"] == 2
