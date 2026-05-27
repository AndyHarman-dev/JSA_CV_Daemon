"""Unit tests for CLI backends: ClaudeCliBackend and GeminiCliBackend.

ClaudeCliBackend: uses subprocess -p mode (no ptyprocess).
  - subprocess.run is patched at jsa.agents.claude_cli.subprocess.run.

GeminiCliBackend: uses subprocess -p mode with -o json (no ptyprocess).
  - subprocess.run is patched at jsa.agents.gemini_cli.subprocess.run.
"""

from __future__ import annotations

import json
import logging
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


def _make_mock_subprocess(stdout: bytes = _FINAL_BYTES, returncode: int = 0) -> MagicMock:
    """Return a MagicMock that looks like subprocess.CompletedProcess."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = b""
    proc.returncode = returncode
    return proc


def _make_gemini_mock_proc(
    response_text: str = _FINAL_TEXT,
    session_id: str = "test-session-uuid",
    returncode: int = 0,
) -> MagicMock:
    """Return a mock subprocess.CompletedProcess with JSON stdout for Gemini."""
    data = {"session_id": session_id, "response": response_text, "stats": {}}
    proc = MagicMock()
    proc.stdout = json.dumps(data).encode()
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
# GeminiCliBackend (rewritten — now uses subprocess -p mode with -o json)
# ---------------------------------------------------------------------------

class TestGeminiCliBackendImport:
    """Test 9 — module import and name attribute."""

    def test_import_does_not_raise(self):
        from jsa.agents.gemini_cli import GeminiCliBackend as GCB
        assert GCB is GeminiCliBackend

    def test_name_attribute_is_gemini_cli(self):
        assert GeminiCliBackend.name == "gemini-cli"


class TestGeminiSessionHandle:
    """Structural test — GeminiSessionHandle fields (subprocess era: no pty field)."""

    def test_fields_id_and_external_id_exist(self):
        handle = GeminiSessionHandle(id="g-id", external_id="ext")
        assert handle.id == "g-id"
        assert handle.external_id == "ext"

    def test_is_subclass_of_session_handle(self):
        handle = GeminiSessionHandle(id="g-id", external_id=None)
        assert isinstance(handle, SessionHandle)

    def test_external_id_can_be_none(self):
        handle = GeminiSessionHandle(id="x", external_id=None)
        assert handle.external_id is None

    def test_no_pty_field(self):
        """GeminiSessionHandle must not have a pty field after subprocess rewrite."""
        handle = GeminiSessionHandle(id="g-id", external_id="ext")
        assert not hasattr(handle, "pty")


class TestGeminiStartSession:
    """Test 10 — start_session returns (GeminiSessionHandle, AgentReply)."""

    async def test_returns_tuple_of_handle_and_reply(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            result = await backend.start_session("sys", "user")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_gemini_session_handle(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, GeminiSessionHandle)

    async def test_reply_kind_is_final(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_subprocess_called_with_skip_trust_flag(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--skip-trust" in cmd

    async def test_subprocess_called_with_session_id_flag(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--session-id" in cmd

    async def test_subprocess_called_with_o_json_flag(self):
        mock_proc = _make_gemini_mock_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "-o" in cmd
        assert "json" in cmd

    async def test_handle_external_id_set_from_json_session_id(self):
        mock_proc = _make_gemini_mock_proc(session_id="from-json-uuid")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id == "from-json-uuid"


class TestGeminiRestoreSession:
    """Tests 11 + 12 — restore_session uses native --resume (no subprocess needed)."""

    async def test_with_external_id_no_subprocess_called(self):
        """Test 11 — with external_id, no subprocess is spawned."""
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "xyz")

        mock_run.assert_not_called()

    async def test_with_external_id_returns_gemini_session_handle(self):
        backend = GeminiCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "xyz")

        assert isinstance(handle, GeminiSessionHandle)

    async def test_with_external_id_preserved_in_handle(self):
        backend = GeminiCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "xyz")

        assert handle.external_id == "xyz"

    async def test_without_external_id_raises_runtime_error(self):
        """Test 12 — without external_id, RuntimeError is raised."""
        backend = GeminiCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.restore_session("sys", [], None)

    async def test_without_external_id_no_subprocess_called(self):
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.restore_session("sys", [], None)

        mock_run.assert_not_called()


class TestBackendForGeminiCli:
    """Test 13 — backend_for("gemini-cli") returns a GeminiCliBackend instance."""

    def test_backend_for_gemini_cli_returns_gemini_cli_backend(self):
        instance = backend_for("gemini-cli")
        assert isinstance(instance, GeminiCliBackend)


class TestGeminiEndSession:
    """end_session is a no-op (subprocess already exited)."""

    async def test_end_session_is_noop(self):
        handle = GeminiSessionHandle(id="h1", external_id="uuid")
        backend = GeminiCliBackend(timeout=5.0)
        # Must not raise
        await backend.end_session(handle)

    async def test_no_subprocess_called(self):
        handle = GeminiSessionHandle(id="h1", external_id="uuid")
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.end_session(handle)

        mock_run.assert_not_called()


class TestGeminiSendMessage:
    """send_message uses --resume mode with -o json, returns AgentReply."""

    async def test_final_reply_returned(self):
        mock_proc = _make_gemini_mock_proc()
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"

    async def test_needs_input_reply_on_need_input_output(self):
        need_input_text = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"
        mock_proc = _make_gemini_mock_proc(response_text=need_input_text)
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_subprocess_called_with_resume_flag(self):
        mock_proc = _make_gemini_mock_proc()
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert "sess-uuid" in cmd

    async def test_subprocess_called_with_skip_trust_flag(self):
        mock_proc = _make_gemini_mock_proc()
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--skip-trust" in cmd

    async def test_subprocess_called_with_p_flag_and_message(self):
        mock_proc = _make_gemini_mock_proc()
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.send_message(handle, "hello gemini")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "hello gemini" in cmd

    async def test_subprocess_called_with_o_json_flag(self):
        mock_proc = _make_gemini_mock_proc()
        handle = GeminiSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "-o" in cmd
        assert "json" in cmd

    async def test_raises_when_external_id_is_none(self):
        handle = GeminiSessionHandle(id="h1", external_id=None)
        backend = GeminiCliBackend(timeout=5.0)

        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "msg")


# ---------------------------------------------------------------------------
# Test 15 — AgentTimeout propagation on subprocess.TimeoutExpired
# ---------------------------------------------------------------------------

class TestAgentTimeoutPropagation:
    """Verify that subprocess.TimeoutExpired is caught inside _run and re-raised as AgentTimeout."""

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
        """GeminiCliBackend.start_session raises AgentTimeout (not subprocess.TimeoutExpired,
        not ProtocolError) when the subprocess times out."""
        with patch(
            "jsa.agents.gemini_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=0.001),
        ):
            backend = GeminiCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")
