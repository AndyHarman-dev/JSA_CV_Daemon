"""Unit tests for Phase 5 CLI backends: ClaudeCliBackend and GeminiCliBackend.

ClaudeCliBackend: now uses subprocess -p mode (no ptyprocess).
  - subprocess.run is patched at jsa.agents.claude_cli.subprocess.run.

GeminiCliBackend: still uses ptyprocess (not the default backend; unchanged).
  - ptyprocess.PtyProcess.spawn is patched as before.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from jsa.agents.base import AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeSessionHandle
from jsa.agents.gemini_cli import GeminiCliBackend, GeminiSessionHandle
from jsa.agents.registry import backend_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_TEXT = "<<<FINAL>>>\nmy cv\n<<<END>>>\n"
_FINAL_BYTES = _FINAL_TEXT.encode()
_NEED_INPUT_BYTES = b"<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"


def _make_mock_pty(read_return: bytes = _FINAL_BYTES) -> MagicMock:
    """Return a MagicMock that looks like a ptyprocess.PtyProcess instance.

    Used only by Gemini tests.
    """
    pty = MagicMock()
    pty.read = MagicMock(return_value=read_return)
    pty.write = MagicMock()
    pty.kill = MagicMock()
    pty.close = MagicMock()
    return pty


def _make_mock_subprocess(stdout: bytes = _FINAL_BYTES, returncode: int = 0) -> MagicMock:
    """Return a MagicMock that looks like subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = b""
    proc.returncode = returncode
    return proc


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
    """Test 2 — ClaudeSessionHandle fields (subprocess era: no pty field)."""

    def test_fields_id_and_external_id_exist(self):
        handle = ClaudeSessionHandle(id="handle-id", external_id="ext-id")
        assert handle.id == "handle-id"
        assert handle.external_id == "ext-id"

    def test_is_subclass_of_session_handle(self):
        handle = ClaudeSessionHandle(id="x", external_id=None)
        assert isinstance(handle, SessionHandle)

    def test_external_id_can_be_none(self):
        handle = ClaudeSessionHandle(id="x", external_id=None)
        assert handle.external_id is None


class TestClaudeStartSession:
    """Test 3 — start_session uses subprocess -p mode."""

    async def test_returns_tuple_of_handle_and_reply(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            result = await backend.start_session("system prompt", "user message")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_claude_session_handle(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_reply_kind_is_final(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_subprocess_called_with_p_flag(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "user message" in cmd

    async def test_subprocess_called_with_session_id_flag(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--session-id" in cmd

    async def test_subprocess_called_with_system_prompt_flag(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("my sys prompt", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--system-prompt" in cmd
        assert "my sys prompt" in cmd

    async def test_handle_external_id_is_set_after_start(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id is not None
        # Should be a valid UUID string
        import uuid
        uuid.UUID(handle.external_id)  # raises ValueError if invalid


class TestClaudeRestoreSessionWithExternalId:
    """Test 4 — restore_session with external_id: no subprocess call, just returns handle."""

    async def test_no_subprocess_called(self):
        with patch("jsa.agents.claude_cli.subprocess.run") as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "abc123")

        mock_run.assert_not_called()

    async def test_returns_claude_session_handle(self):
        backend = ClaudeCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "abc123")

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_external_id_preserved_in_handle(self):
        backend = ClaudeCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "abc123")

        assert handle.external_id == "abc123"


class TestClaudeRestoreSessionWithoutExternalId:
    """Test 5 — restore_session without external_id raises RuntimeError."""

    async def test_raises_runtime_error_when_no_external_id(self):
        backend = ClaudeCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.restore_session("sys", [], None)

    async def test_no_subprocess_called_before_raise(self):
        with patch("jsa.agents.claude_cli.subprocess.run") as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.restore_session("sys", [], None)

        mock_run.assert_not_called()


class TestClaudeSendMessage:
    """Test 6 — send_message uses --resume mode, returns AgentReply."""

    async def test_needs_input_reply_on_need_input_output(self):
        mock_proc = _make_mock_subprocess(_NEED_INPUT_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_subprocess_called_with_resume_flag(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert "sess-uuid" in cmd

    async def test_subprocess_called_with_p_flag_and_message(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "my message" in cmd

    async def test_no_system_prompt_on_resume(self):
        """send_message must NOT pass --system-prompt (Claude CLI preserves it via session)."""
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "--system-prompt" not in cmd

    async def test_final_reply_returned_on_final_output(self):
        mock_proc = _make_mock_subprocess(_FINAL_BYTES)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.subprocess.run", return_value=mock_proc):
            backend = ClaudeCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"
        assert "my cv" in reply.content

    async def test_raises_when_external_id_is_none(self):
        handle = ClaudeSessionHandle(id="h1", external_id=None)
        backend = ClaudeCliBackend(timeout=5.0)

        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "msg")


class TestClaudeEndSession:
    """Test 7 — end_session is a no-op (subprocess already exited)."""

    async def test_end_session_is_noop(self):
        handle = ClaudeSessionHandle(id="h1", external_id="uuid")
        backend = ClaudeCliBackend(timeout=5.0)
        # Must not raise
        await backend.end_session(handle)

    async def test_no_subprocess_called(self):
        handle = ClaudeSessionHandle(id="h1", external_id="uuid")
        with patch("jsa.agents.claude_cli.subprocess.run") as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.end_session(handle)

        mock_run.assert_not_called()


class TestBackendForClaudeCli:
    """Test 8 — backend_for("claude-cli") returns a ClaudeCliBackend instance."""

    def test_backend_for_claude_cli_returns_claude_cli_backend(self):
        instance = backend_for("claude-cli")
        assert isinstance(instance, ClaudeCliBackend)


# ---------------------------------------------------------------------------
# GeminiCliBackend (unchanged — still uses ptyprocess)
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
# Test 15 — AgentTimeout propagation on subprocess.TimeoutExpired
# ---------------------------------------------------------------------------

class TestAgentTimeoutPropagation:
    """Verify that subprocess.TimeoutExpired is caught inside _run and re-raised as AgentTimeout.

    The pty-era _pty_common.asyncio.wait_for path is no longer used by ClaudeCliBackend.
    """

    async def test_claude_cli_backend_raises_agent_timeout(self):
        """ClaudeCliBackend.start_session raises AgentTimeout (not subprocess.TimeoutExpired,
        not ProtocolError) when the subprocess times out."""
        with patch(
            "jsa.agents.claude_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=0.001),
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
