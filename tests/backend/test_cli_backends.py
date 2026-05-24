"""Unit tests for Phase 5 CLI backends: ClaudeCliBackend and GeminiCliBackend.

Real pty processes are never spawned. The only mock is ptyprocess.PtyProcess.spawn
(the external OS/subprocess boundary). All JSA-internal logic is exercised without
patching.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from unittest.mock import MagicMock, patch

import pytest

from jsa.agents.base import AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeSessionHandle
from jsa.agents.gemini_cli import GeminiCliBackend, GeminiSessionHandle
from jsa.agents.registry import backend_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_BYTES = b"<<<FINAL>>>\nmy cv\n<<<END>>>\n"
_NEED_INPUT_BYTES = b"<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"


def _make_mock_pty(read_return: bytes = _FINAL_BYTES) -> MagicMock:
    """Return a MagicMock that looks like a ptyprocess.PtyProcess instance.

    `.read()` returns the given bytes synchronously (called in an executor thread
    by _read_until_sentinel — must NOT be an AsyncMock).
    """
    pty = MagicMock()
    pty.read = MagicMock(return_value=read_return)
    pty.write = MagicMock()
    pty.kill = MagicMock()
    pty.close = MagicMock()
    return pty


# ---------------------------------------------------------------------------
# 14 — AgentTimeout is importable from jsa.agents.base and is an Exception
# ---------------------------------------------------------------------------

class TestAgentTimeout:
    def test_agent_timeout_is_importable(self):
        from jsa.agents.base import AgentTimeout as AT
        assert AT is AgentTimeout

    def test_agent_timeout_is_exception_subclass(self):
        assert issubclass(AgentTimeout, Exception)

    def test_agent_timeout_can_be_raised_and_caught(self):
        with pytest.raises(AgentTimeout):
            raise AgentTimeout("timed out")


# ---------------------------------------------------------------------------
# ClaudeCliBackend
# ---------------------------------------------------------------------------

class TestClaudeCliBackendImport:
    """Test 1 — module import and name attribute."""

    def test_import_does_not_raise(self):
        from jsa.agents.claude_cli import ClaudeCliBackend as CCB
        assert CCB is ClaudeCliBackend

    def test_name_attribute_is_claude_cli(self):
        assert ClaudeCliBackend.name == "claude-cli"


class TestClaudeSessionHandle:
    """Test 2 — ClaudeSessionHandle fields."""

    def test_fields_id_external_id_pty_exist(self):
        mock_pty = _make_mock_pty()
        handle = ClaudeSessionHandle(id="handle-id", external_id="ext-id", pty=mock_pty)
        assert handle.id == "handle-id"
        assert handle.external_id == "ext-id"
        assert handle.pty is mock_pty

    def test_is_subclass_of_session_handle(self):
        mock_pty = _make_mock_pty()
        handle = ClaudeSessionHandle(id="x", external_id=None, pty=mock_pty)
        assert isinstance(handle, SessionHandle)

    def test_external_id_can_be_none(self):
        mock_pty = _make_mock_pty()
        handle = ClaudeSessionHandle(id="x", external_id=None, pty=mock_pty)
        assert handle.external_id is None


class TestClaudeStartSession:
    """Test 3 — start_session spawns pty and returns (ClaudeSessionHandle, AgentReply)."""

    async def test_returns_tuple_of_handle_and_reply(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = ClaudeCliBackend(timeout=5.0)
            result = await backend.start_session("system prompt", "user message")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_claude_session_handle(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_reply_kind_is_final(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_pty_write_was_called(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        mock_pty.write.assert_called()

    async def test_pty_spawn_was_called(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        mock_spawn.assert_called_once()


class TestClaudeRestoreSessionWithExternalId:
    """Test 4 — restore_session with external_id uses --resume flag."""

    async def test_spawn_called_with_resume_flag(self):
        mock_pty = _make_mock_pty()  # no read needed for native resume path
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "abc123")

        call_args = mock_spawn.call_args.args[0]
        assert "--resume" in call_args
        assert "abc123" in call_args

    async def test_returns_claude_session_handle(self):
        mock_pty = _make_mock_pty()
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            handle = await backend.restore_session("sys", [], "abc123")

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_external_id_preserved_in_handle(self):
        mock_pty = _make_mock_pty()
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            handle = await backend.restore_session("sys", [], "abc123")

        assert handle.external_id == "abc123"


class TestClaudeRestoreSessionWithoutExternalId:
    """Test 5 — restore_session without external_id falls back (no --resume)."""

    async def test_resume_flag_not_in_spawn_args(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)  # fallback path reads from pty
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], None)

        call_args = mock_spawn.call_args.args[0]
        assert "--resume" not in call_args

    async def test_returns_claude_session_handle(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            handle = await backend.restore_session("sys", [], None)

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_history_turns_are_written_to_pty(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        history = [
            HistoryTurn(role="user", content="hello"),
            HistoryTurn(role="assistant", content="world"),
        ]
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.restore_session("sys", history, None)

        # write() must have been called to push the combined history message
        mock_pty.write.assert_called()


class TestClaudeSendMessage:
    """Test 6 — send_message writes to pty and returns AgentReply."""

    async def test_needs_input_reply_on_need_input_output(self):
        mock_pty = _make_mock_pty(_NEED_INPUT_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_pty_write_called_with_message(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        await backend.send_message(handle, "my message")

        mock_pty.write.assert_called_once()
        # The written bytes should contain the message text
        written = mock_pty.write.call_args.args[0]
        assert b"my message" in written

    async def test_final_reply_returned_on_final_output(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"
        assert "my cv" in reply.content


class TestClaudeEndSession:
    """Test 7 — end_session terminates pty."""

    async def test_pty_close_is_called(self):
        mock_pty = _make_mock_pty()
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        await backend.end_session(handle)

        mock_pty.close.assert_called()

    async def test_pty_kill_is_called_with_sigterm(self):
        mock_pty = _make_mock_pty()
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        await backend.end_session(handle)

        mock_pty.kill.assert_called()
        call_args = mock_pty.kill.call_args.args
        assert signal.SIGTERM in call_args

    async def test_end_session_does_not_raise_if_pty_errors(self):
        """end_session wraps kill/close in try/except — errors must not propagate."""
        mock_pty = _make_mock_pty()
        mock_pty.kill.side_effect = OSError("process gone")
        mock_pty.close.side_effect = OSError("already closed")
        handle = ClaudeSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = ClaudeCliBackend(timeout=5.0)
        # Must not raise
        await backend.end_session(handle)


class TestBackendForClaudeCli:
    """Test 8 — backend_for("claude-cli") returns a ClaudeCliBackend instance."""

    def test_backend_for_claude_cli_returns_claude_cli_backend(self):
        instance = backend_for("claude-cli")
        assert isinstance(instance, ClaudeCliBackend)


# ---------------------------------------------------------------------------
# GeminiCliBackend
# ---------------------------------------------------------------------------

class TestGeminiCliBackendImport:
    """Test 9 — module import and name attribute."""

    def test_import_does_not_raise(self):
        from jsa.agents.gemini_cli import GeminiCliBackend as GCB
        assert GCB is GeminiCliBackend

    def test_name_attribute_is_gemini_cli(self):
        assert GeminiCliBackend.name == "gemini-cli"


class TestGeminiStartSession:
    """Test 10 — start_session returns (GeminiSessionHandle, AgentReply)."""

    async def test_returns_tuple_of_handle_and_reply(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            result = await backend.start_session("sys", "user")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_gemini_session_handle(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, GeminiSessionHandle)

    async def test_reply_is_agent_reply_with_correct_kind(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"
        assert "my cv" in reply.content

    async def test_pty_write_called_on_start(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        mock_pty.write.assert_called()


class TestGeminiRestoreSession:
    """Tests 11 + 12 — restore_session always uses history-replay (no --resume), logs WARNING."""

    async def test_resume_flag_not_used_even_with_external_id(self):
        """Test 11 — Gemini never passes --resume regardless of external_id."""
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "xyz")

        call_args = mock_spawn.call_args.args[0]
        assert "--resume" not in call_args

    async def test_resume_flag_not_used_with_none_external_id(self):
        """Test 11 (None path) — still no --resume."""
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty) as mock_spawn:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], None)

        call_args = mock_spawn.call_args.args[0]
        assert "--resume" not in call_args

    async def test_warning_logged_on_restore(self, caplog):
        """Test 12 — WARNING is emitted from jsa.agents.gemini_cli on restore_session."""
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.gemini_cli"):
                await backend.restore_session("sys", [], "xyz")

        warning_records = [
            r for r in caplog.records
            if r.name == "jsa.agents.gemini_cli" and r.levelno == logging.WARNING
        ]
        assert warning_records, "Expected a WARNING from jsa.agents.gemini_cli"

    async def test_warning_logged_even_when_external_id_is_none(self, caplog):
        """Test 12 (None path) — WARNING still emitted when no external_id provided."""
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.gemini_cli"):
                await backend.restore_session("sys", [], None)

        warning_records = [
            r for r in caplog.records
            if r.name == "jsa.agents.gemini_cli" and r.levelno == logging.WARNING
        ]
        assert warning_records, "Expected a WARNING from jsa.agents.gemini_cli"

    async def test_returns_gemini_session_handle(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            handle = await backend.restore_session("sys", [], "xyz")

        assert isinstance(handle, GeminiSessionHandle)

    async def test_history_turns_written_to_pty(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        history = [
            HistoryTurn(role="user", content="tell me more"),
            HistoryTurn(role="assistant", content="sure thing"),
        ]
        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty):
            backend = GeminiCliBackend(timeout=5.0)
            await backend.restore_session("sys", history, None)

        mock_pty.write.assert_called()


class TestBackendForGeminiCli:
    """Test 13 — backend_for("gemini-cli") returns a GeminiCliBackend instance."""

    def test_backend_for_gemini_cli_returns_gemini_cli_backend(self):
        instance = backend_for("gemini-cli")
        assert isinstance(instance, GeminiCliBackend)


# ---------------------------------------------------------------------------
# GeminiSessionHandle structural test
# ---------------------------------------------------------------------------

class TestGeminiSessionHandle:
    def test_fields_id_external_id_pty_exist(self):
        mock_pty = _make_mock_pty()
        handle = GeminiSessionHandle(id="g-id", external_id="ext", pty=mock_pty)
        assert handle.id == "g-id"
        assert handle.external_id == "ext"
        assert handle.pty is mock_pty

    def test_is_subclass_of_session_handle(self):
        mock_pty = _make_mock_pty()
        handle = GeminiSessionHandle(id="g-id", external_id=None, pty=mock_pty)
        assert isinstance(handle, SessionHandle)


# ---------------------------------------------------------------------------
# GeminiCliBackend end_session
# ---------------------------------------------------------------------------

class TestGeminiEndSession:
    async def test_pty_close_is_called(self):
        mock_pty = _make_mock_pty()
        handle = GeminiSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = GeminiCliBackend(timeout=5.0)
        await backend.end_session(handle)

        mock_pty.close.assert_called()

    async def test_pty_kill_is_called_with_sigterm(self):
        mock_pty = _make_mock_pty()
        handle = GeminiSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = GeminiCliBackend(timeout=5.0)
        await backend.end_session(handle)

        mock_pty.kill.assert_called()
        call_args = mock_pty.kill.call_args.args
        assert signal.SIGTERM in call_args

    async def test_end_session_does_not_raise_if_pty_errors(self):
        mock_pty = _make_mock_pty()
        mock_pty.kill.side_effect = OSError("process gone")
        mock_pty.close.side_effect = OSError("already closed")
        handle = GeminiSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = GeminiCliBackend(timeout=5.0)
        # Must not raise — both calls are wrapped in try/except
        await backend.end_session(handle)


# ---------------------------------------------------------------------------
# GeminiCliBackend send_message
# ---------------------------------------------------------------------------

class TestGeminiSendMessage:
    async def test_final_reply_returned(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        handle = GeminiSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = GeminiCliBackend(timeout=5.0)
        reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"

    async def test_pty_write_called_with_message(self):
        mock_pty = _make_mock_pty(_FINAL_BYTES)
        handle = GeminiSessionHandle(id="h1", external_id=None, pty=mock_pty)

        backend = GeminiCliBackend(timeout=5.0)
        await backend.send_message(handle, "hello gemini")

        mock_pty.write.assert_called_once()
        written = mock_pty.write.call_args.args[0]
        assert b"hello gemini" in written


# ---------------------------------------------------------------------------
# Test 15 — AgentTimeout propagation on asyncio.TimeoutError
# ---------------------------------------------------------------------------

class TestAgentTimeoutPropagation:
    """Verify that asyncio.TimeoutError inside _read_until_sentinel is caught before
    OSError and re-raised as AgentTimeout — not swallowed, not leaked as TimeoutError,
    and not re-raised as ProtocolError.

    Strategy: patch `jsa.agents._pty_common.asyncio.wait_for` so it raises
    asyncio.TimeoutError immediately. Both ClaudeCliBackend and GeminiCliBackend
    call _read_until_sentinel during start_session, so we exercise both.
    """

    async def test_claude_cli_backend_raises_agent_timeout(self):
        """ClaudeCliBackend.start_session raises AgentTimeout (not asyncio.TimeoutError,
        not ProtocolError) when the pty read times out."""
        mock_pty = _make_mock_pty()  # read bytes are irrelevant — wait_for fires first

        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty), \
             patch(
                 "jsa.agents._pty_common.asyncio.wait_for",
                 side_effect=asyncio.TimeoutError,
             ):
            backend = ClaudeCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_gemini_cli_backend_raises_agent_timeout(self):
        """GeminiCliBackend.start_session raises AgentTimeout (not asyncio.TimeoutError,
        not ProtocolError) when the pty read times out."""
        mock_pty = _make_mock_pty()  # read bytes are irrelevant — wait_for fires first

        with patch("ptyprocess.PtyProcess.spawn", return_value=mock_pty), \
             patch(
                 "jsa.agents._pty_common.asyncio.wait_for",
                 side_effect=asyncio.TimeoutError,
             ):
            backend = GeminiCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")
