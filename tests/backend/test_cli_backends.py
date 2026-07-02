"""Unit tests for CLI backends: ClaudeCliBackend and GoogleCliBackend.

Both backends spawn their subprocess via the shared `run_killable` seam
(jsa/agents/_subprocess.py) so the whole process group can be killed on
cancel/timeout — see jsa/agents/_subprocess.py and test_subprocess_killable.py
for the killability contract itself. These unit tests patch that seam directly
(an AsyncMock returning a plain (returncode, stdout_bytes, stderr_bytes)
tuple) rather than faking a full asyncio subprocess object.

ClaudeCliBackend: `run_killable` is patched at jsa.agents.claude_cli.run_killable.
GoogleCliBackend: `run_killable` is patched at jsa.agents.google_cli.run_killable.
  - Session ID extracted from --log-file; patched via _extract_conversation_id in unit tests.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from jsa.agents.base import AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeSessionHandle
from jsa.agents.google_cli import GoogleCliBackend, GoogleSessionHandle
from jsa.agents.registry import backend_for
from tests.backend.fakes.run_killable_result import ok as _run_killable_ok


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_TEXT = "<<<FINAL>>>\nmy cv\n<<<END>>>\n"
_FINAL_BYTES = _FINAL_TEXT.encode()
_NEED_INPUT_BYTES = b"<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"


def _ok(stdout: bytes = _FINAL_BYTES, returncode: int = 0, stderr: bytes = b"") -> tuple[int, bytes, bytes]:
    """Return a (returncode, stdout, stderr) tuple shaped like run_killable's return value."""
    return _run_killable_ok(stdout, returncode, stderr)


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
    """Test 3 — start_session uses the killable async subprocess seam."""

    async def test_returns_tuple_of_handle_and_reply(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = ClaudeCliBackend(timeout=5.0)
            result = await backend.start_session("system prompt", "user message")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_claude_session_handle(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = ClaudeCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, ClaudeSessionHandle)

    async def test_reply_kind_is_final(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = ClaudeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_subprocess_called_with_p_flag(self):
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "user message" in cmd

    async def test_subprocess_called_with_session_id_flag(self):
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--session-id" in cmd

    async def test_subprocess_called_with_system_prompt_flag(self):
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("my sys prompt", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--system-prompt" in cmd
        assert "my sys prompt" in cmd

    async def test_subprocess_called_with_no_tools_flag(self):
        """start_session must disable all tools — these are text-only sentinel turns,
        never a coding/file-writing session (regression guard for stray file writes)."""
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--tools" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""

    async def test_handle_external_id_is_set_after_start(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = ClaudeCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id is not None
        # Should be a valid UUID string
        import uuid
        uuid.UUID(handle.external_id)  # raises ValueError if invalid


class TestClaudeRestoreSessionWithExternalId:
    """Test 4 — restore_session with external_id: no subprocess call, just returns handle."""

    async def test_no_subprocess_called(self):
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock()) as mock_run:
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
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock()) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.restore_session("sys", [], None)

        mock_run.assert_not_called()


class TestClaudeSendMessage:
    """Test 6 — send_message uses --resume mode, returns AgentReply."""

    async def test_needs_input_reply_on_need_input_output(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok(_NEED_INPUT_BYTES))):
            backend = ClaudeCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_subprocess_called_with_resume_flag(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert "sess-uuid" in cmd

    async def test_subprocess_called_with_p_flag_and_message(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "my message" in cmd

    async def test_no_system_prompt_on_resume(self):
        """send_message must NOT pass --system-prompt (Claude CLI preserves it via session)."""
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "--system-prompt" not in cmd

    async def test_subprocess_called_with_no_tools_flag(self):
        """send_message must disable all tools — same regression guard as start_session."""
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.claude_cli.run_killable", new=mock_run):
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "--tools" in cmd
        assert cmd[cmd.index("--tools") + 1] == ""

    async def test_final_reply_returned_on_final_output(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=_ok())):
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
        with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock()) as mock_run:
            backend = ClaudeCliBackend(timeout=5.0)
            await backend.end_session(handle)

        mock_run.assert_not_called()


class TestBackendForClaudeCli:
    """Test 8 — backend_for("claude-cli") returns a ClaudeCliBackend instance."""

    def test_backend_for_claude_cli_returns_claude_cli_backend(self):
        instance = backend_for("claude-cli")
        assert isinstance(instance, ClaudeCliBackend)


# ---------------------------------------------------------------------------
# GoogleCliBackend (agy subprocess, plain-text output)
# ---------------------------------------------------------------------------

class TestGoogleCliBackendImport:
    """Test 9 — module import and name attribute."""

    def test_import_does_not_raise(self):
        from jsa.agents.google_cli import GoogleCliBackend as GCB
        assert GCB is GoogleCliBackend

    def test_name_attribute_is_google_cli(self):
        assert GoogleCliBackend.name == "google-cli"


class TestGoogleSessionHandle:
    """Structural test — GoogleSessionHandle fields (subprocess era: no pty field)."""

    def test_fields_id_and_external_id_exist(self):
        handle = GoogleSessionHandle(id="g-id", external_id="ext")
        assert handle.id == "g-id"
        assert handle.external_id == "ext"

    def test_is_subclass_of_session_handle(self):
        handle = GoogleSessionHandle(id="g-id", external_id=None)
        assert isinstance(handle, SessionHandle)

    def test_external_id_can_be_none(self):
        handle = GoogleSessionHandle(id="x", external_id=None)
        assert handle.external_id is None

    def test_no_pty_field(self):
        """GoogleSessionHandle must not have a pty field after subprocess rewrite."""
        handle = GoogleSessionHandle(id="g-id", external_id="ext")
        assert not hasattr(handle, "pty")


class TestGoogleStartSession:
    """Test 10 — start_session returns (GoogleSessionHandle, AgentReply)."""

    async def test_returns_tuple_of_handle_and_reply(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            result = await backend.start_session("sys", "user")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_google_session_handle(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, GoogleSessionHandle)

    async def test_reply_kind_is_final(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_subprocess_called_with_dangerously_skip_permissions_flag(self):
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.google_cli.run_killable", new=mock_run), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--dangerously-skip-permissions" in cmd

    async def test_subprocess_called_with_log_file_flag(self):
        mock_run = AsyncMock(return_value=_ok())
        with patch("jsa.agents.google_cli.run_killable", new=mock_run), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--log-file" in cmd

    async def test_handle_external_id_set_from_log_extraction(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="extracted-conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id == "extracted-conv-uuid"


class TestGoogleRestoreSession:
    """Tests 11 + 12 — restore_session uses native --resume (no subprocess needed)."""

    async def test_with_external_id_no_subprocess_called(self):
        """Test 11 — with external_id, no subprocess is spawned."""
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock()) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "xyz")

        mock_run.assert_not_called()

    async def test_with_external_id_returns_google_session_handle(self):
        backend = GoogleCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "xyz")

        assert isinstance(handle, GoogleSessionHandle)

    async def test_with_external_id_preserved_in_handle(self):
        backend = GoogleCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "xyz")

        assert handle.external_id == "xyz"

    async def test_without_external_id_raises_runtime_error(self):
        """Test 12 — without external_id, RuntimeError is raised."""
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.restore_session("sys", [], None)

    async def test_without_external_id_no_subprocess_called(self):
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock()) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.restore_session("sys", [], None)

        mock_run.assert_not_called()


class TestBackendForGoogleCli:
    """Test 13 — backend_for("google-cli") returns a GoogleCliBackend instance."""

    def test_backend_for_google_cli_returns_google_cli_backend(self):
        instance = backend_for("google-cli")
        assert isinstance(instance, GoogleCliBackend)


class TestGoogleEndSession:
    """end_session is a no-op (subprocess already exited)."""

    async def test_end_session_is_noop(self):
        handle = GoogleSessionHandle(id="h1", external_id="uuid")
        backend = GoogleCliBackend(timeout=5.0)
        # Must not raise
        await backend.end_session(handle)

    async def test_no_subprocess_called(self):
        handle = GoogleSessionHandle(id="h1", external_id="uuid")
        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock()) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend.end_session(handle)

        mock_run.assert_not_called()


class TestGoogleSendMessage:
    """send_message uses --conversation mode, plain-text output, returns AgentReply."""

    async def test_final_reply_returned(self):
        handle = GoogleSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok())):
            backend = GoogleCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"

    async def test_needs_input_reply_on_need_input_output(self):
        need_input_bytes = b"<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"
        handle = GoogleSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.google_cli.run_killable", new=AsyncMock(return_value=_ok(need_input_bytes))):
            backend = GoogleCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_subprocess_called_with_conversation_flag(self):
        handle = GoogleSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.google_cli.run_killable", new=mock_run):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--conversation" in cmd
        assert "sess-uuid" in cmd

    async def test_subprocess_called_with_dangerously_skip_permissions_flag(self):
        handle = GoogleSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.google_cli.run_killable", new=mock_run):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--dangerously-skip-permissions" in cmd

    async def test_subprocess_called_with_p_flag_and_message(self):
        handle = GoogleSessionHandle(id="h1", external_id="sess-uuid")
        mock_run = AsyncMock(return_value=_ok())

        with patch("jsa.agents.google_cli.run_killable", new=mock_run):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.send_message(handle, "hello agy")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "hello agy" in cmd

    async def test_raises_when_external_id_is_none(self):
        handle = GoogleSessionHandle(id="h1", external_id=None)
        backend = GoogleCliBackend(timeout=5.0)

        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "msg")


# ---------------------------------------------------------------------------
# Test 15 — AgentTimeout propagation from run_killable
# ---------------------------------------------------------------------------

class TestAgentTimeoutPropagation:
    """Verify AgentTimeout raised by run_killable (on subprocess timeout) propagates
    unchanged through _run — not swallowed, not rewrapped as ProtocolError."""

    async def test_claude_cli_backend_raises_agent_timeout(self):
        """ClaudeCliBackend.start_session raises AgentTimeout (not ProtocolError)
        when run_killable times out."""
        with patch(
            "jsa.agents.claude_cli.run_killable",
            new=AsyncMock(side_effect=AgentTimeout("claude CLI timed out after 0.001s")),
        ):
            backend = ClaudeCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_google_cli_backend_raises_agent_timeout(self):
        """GoogleCliBackend.start_session raises AgentTimeout (not ProtocolError)
        when run_killable times out."""
        with patch(
            "jsa.agents.google_cli.run_killable",
            new=AsyncMock(side_effect=AgentTimeout("agy CLI timed out after 0.001s")),
        ):
            backend = GoogleCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")
