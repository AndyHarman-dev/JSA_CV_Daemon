"""Unit tests for OpenCodeCliBackend.

OpenCodeCliBackend: uses subprocess -p mode (no ptyprocess).
  - subprocess.run is patched at jsa.agents.opencode_cli.subprocess.run.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from jsa.agents.base import AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.opencode_cli import (
    OpenCodeCliBackend,
    OpenCodeCliError,
    OpenCodeSessionExpiredError,
    OpenCodeSessionHandle,
)
from jsa.agents.protocol import ProtocolError
from jsa.agents.registry import backend_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_TEXT = "<<<FINAL>>>\nmy cv\n<<<END>>>\n"


def _make_mock_proc(
    response: str = _FINAL_TEXT,
    returncode: int = 0,
    stderr: bytes = b"",
) -> MagicMock:
    """Return a MagicMock resembling subprocess.CompletedProcess with plain-text stdout."""
    proc = MagicMock()
    proc.stdout = response.encode()
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# 1 — Import & name attribute
# ---------------------------------------------------------------------------

class TestOpenCodeCliBackendImport:
    def test_import_does_not_raise(self):
        from jsa.agents.opencode_cli import OpenCodeCliBackend as OCB
        assert OCB is OpenCodeCliBackend

    def test_name_attribute_is_opencode_cli(self):
        assert OpenCodeCliBackend.name == "opencode-cli"


# ---------------------------------------------------------------------------
# 2 — OpenCodeSessionHandle fields
# ---------------------------------------------------------------------------

class TestOpenCodeSessionHandle:
    def test_fields_id_and_external_id_exist(self):
        handle = OpenCodeSessionHandle(id="h-id", external_id="ext")
        assert handle.id == "h-id"
        assert handle.external_id == "ext"

    def test_is_subclass_of_session_handle(self):
        handle = OpenCodeSessionHandle(id="h-id", external_id=None)
        assert isinstance(handle, SessionHandle)

    def test_external_id_can_be_none(self):
        handle = OpenCodeSessionHandle(id="x", external_id=None)
        assert handle.external_id is None


# ---------------------------------------------------------------------------
# 3 — start_session: uses --session <uuid> -p "sys\n\nuser" -m <model>
# ---------------------------------------------------------------------------

class TestOpenCodeStartSession:
    async def test_returns_tuple_of_handle_and_reply(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            result = await backend.start_session("system prompt", "user message")

        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_is_opencode_session_handle(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            handle, _reply = await backend.start_session("sys", "user")

        assert isinstance(handle, OpenCodeSessionHandle)

    async def test_reply_kind_is_final(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert reply.kind == "final"

    async def test_reply_content_contains_expected_text(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            _handle, reply = await backend.start_session("sys", "user")

        assert "my cv" in reply.content

    async def test_subprocess_called_with_session_flag(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user message")

        cmd = mock_run.call_args.args[0]
        assert "--session" in cmd

    async def test_subprocess_called_with_p_flag_and_combined_prompt(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.start_session("my sys prompt", "user message")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        # System prompt and user message are combined
        p_idx = cmd.index("-p")
        combined = cmd[p_idx + 1]
        assert "my sys prompt" in combined
        assert "user message" in combined

    async def test_subprocess_called_with_model_flag(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        cmd = mock_run.call_args.args[0]
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "opencode/nemotron-3-ultra-free"

    async def test_handle_external_id_is_set_after_start(self):
        mock_proc = _make_mock_proc()
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id is not None
        # Should be a valid UUID string
        import uuid
        uuid.UUID(handle.external_id)  # raises ValueError if invalid


# ---------------------------------------------------------------------------
# 4 — restore_session with external_id
# ---------------------------------------------------------------------------

class TestOpenCodeRestoreSessionWithExternalId:
    async def test_no_subprocess_called(self):
        with patch("jsa.agents.opencode_cli.subprocess.run") as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.restore_session("sys", [], "abc123")

        mock_run.assert_not_called()

    async def test_returns_opencode_session_handle(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "abc123")

        assert isinstance(handle, OpenCodeSessionHandle)

    async def test_external_id_preserved_in_handle(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        handle = await backend.restore_session("sys", [], "abc123")

        assert handle.external_id == "abc123"


# ---------------------------------------------------------------------------
# 5 — restore_session without external_id raises RuntimeError
# ---------------------------------------------------------------------------

class TestOpenCodeRestoreSessionWithoutExternalId:
    async def test_raises_runtime_error_when_no_external_id(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.restore_session("sys", [], None)

    async def test_no_subprocess_called_before_raise(self):
        with patch("jsa.agents.opencode_cli.subprocess.run") as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.restore_session("sys", [], None)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 6 — send_message: uses --session <external_id> -p <msg> -m <model>
# ---------------------------------------------------------------------------

class TestOpenCodeSendMessage:
    async def test_final_reply_returned(self):
        mock_proc = _make_mock_proc()
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "text")

        assert reply.kind == "final"

    async def test_needs_input_reply_on_need_input_output(self):
        need_input_text = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>\n"
        mock_proc = _make_mock_proc(response=need_input_text)
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            reply = await backend.send_message(handle, "my answer")

        assert reply.kind == "needs_input"
        assert "target role" in reply.question

    async def test_subprocess_called_with_session_flag(self):
        mock_proc = _make_mock_proc()
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.send_message(handle, "my message")

        cmd = mock_run.call_args.args[0]
        assert "--session" in cmd
        assert "sess-uuid" in cmd

    async def test_subprocess_called_with_p_flag_and_message(self):
        mock_proc = _make_mock_proc()
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.send_message(handle, "hello opencode")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd
        assert "hello opencode" in cmd

    async def test_subprocess_called_with_model_flag(self):
        mock_proc = _make_mock_proc()
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "opencode/nemotron-3-ultra-free"

    async def test_no_system_prompt_on_resume(self):
        """send_message must NOT pass system prompt separately — opencode uses session persistence."""
        mock_proc = _make_mock_proc()
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")

        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.send_message(handle, "text")

        cmd = mock_run.call_args.args[0]
        assert "--system-prompt" not in cmd

    async def test_raises_when_external_id_is_none(self):
        handle = OpenCodeSessionHandle(id="h1", external_id=None)
        backend = OpenCodeCliBackend(timeout=5.0)

        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "msg")


# ---------------------------------------------------------------------------
# 7 — end_session is a no-op
# ---------------------------------------------------------------------------

class TestOpenCodeEndSession:
    async def test_end_session_is_noop(self):
        handle = OpenCodeSessionHandle(id="h1", external_id="uuid")
        backend = OpenCodeCliBackend(timeout=5.0)
        # Must not raise
        await backend.end_session(handle)

    async def test_no_subprocess_called(self):
        handle = OpenCodeSessionHandle(id="h1", external_id="uuid")
        with patch("jsa.agents.opencode_cli.subprocess.run") as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend.end_session(handle)

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 8 — AgentTimeout propagation on subprocess.TimeoutExpired
# ---------------------------------------------------------------------------

class TestAgentTimeoutPropagation:
    async def test_opencode_cli_backend_raises_agent_timeout(self):
        """OpenCodeCliBackend.start_session raises AgentTimeout when the subprocess times out."""
        with patch(
            "jsa.agents.opencode_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["opencode"], timeout=0.001),
        ):
            backend = OpenCodeCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.start_session("sys", "msg")

    async def test_opencode_cli_send_message_raises_agent_timeout(self):
        handle = OpenCodeSessionHandle(id="h1", external_id="sess-uuid")
        with patch(
            "jsa.agents.opencode_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["opencode"], timeout=0.001),
        ):
            backend = OpenCodeCliBackend(timeout=0.001)
            with pytest.raises(AgentTimeout):
                await backend.send_message(handle, "msg")


# ---------------------------------------------------------------------------
# 9 — _parse_with_nudge: pass-through when sentinel is present
# ---------------------------------------------------------------------------

class TestParseWithNudgePassThrough:
    async def test_returns_reply_immediately_when_sentinel_present(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        assert reply.kind == "final"
        assert reply.content == "hello"

    async def test_no_subprocess_called_when_sentinel_present(self):
        with patch("jsa.agents.opencode_cli.subprocess.run") as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 10 — _parse_with_nudge: sends nudge when sentinel is missing
# ---------------------------------------------------------------------------

class TestParseWithNudgeRetry:
    async def test_returns_nudge_reply_with_final_kind(self):
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc):
            backend = OpenCodeCliBackend(timeout=5.0)
            reply = await backend._parse_with_nudge("sess-id", "this has no sentinel")

        assert reply.kind == "final"
        assert reply.content == "nudged"

    async def test_nudge_cmd_uses_session_flag(self):
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "--session" in cmd
        assert "sess-id" in cmd

    async def test_nudge_cmd_uses_p_flag(self):
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd

    async def test_nudge_cmd_p_arg_mentions_missing_sentinel(self):
        """The -p argument for the nudge should mention 'missing' or 'sentinel block'."""
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        nudge_text = cmd[p_idx + 1]
        assert "missing" in nudge_text or "sentinel block" in nudge_text

    async def test_nudge_subprocess_called_exactly_once(self):
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        assert mock_run.call_count == 1

    async def test_nudge_cmd_uses_model_flag(self):
        nudge_proc = _make_mock_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "-m" in cmd
        m_idx = cmd.index("-m")
        assert cmd[m_idx + 1] == "opencode/nemotron-3-ultra-free"


# ---------------------------------------------------------------------------
# 11 — _parse_with_nudge: re-raises non-sentinel ProtocolError without nudging
# ---------------------------------------------------------------------------

class TestParseWithNudgeNonSentinelError:
    """An unterminated block should be re-raised immediately — no nudge subprocess call."""

    async def test_unterminated_block_raises_immediately(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        with pytest.raises(ProtocolError, match="unterminated block"):
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")

    async def test_no_nudge_subprocess_for_unterminated_block(self):
        with patch("jsa.agents.opencode_cli.subprocess.run") as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 12 — _parse_with_nudge: nudge reply also missing sentinel → ProtocolError propagated
# ---------------------------------------------------------------------------

class TestParseWithNudgeInfiniteRetry:
    """When the nudge reply is also sentinel-less, ProtocolError propagates. No infinite retry."""

    async def test_protocol_error_raised_when_nudge_reply_also_has_no_sentinel(self):
        nudge_proc = _make_mock_proc(response="still no sentinel here")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "no sentinel here")

    async def test_guard_against_infinite_retry(self):
        """Subprocess.run is called exactly once (the nudge), not recursively."""
        nudge_proc = _make_mock_proc(response="still no sentinel")
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = OpenCodeCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("s", "original missing sentinel")

        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# 13 — backend_for("opencode-cli") returns an OpenCodeCliBackend instance
# ---------------------------------------------------------------------------

class TestBackendForOpenCodeCli:
    def test_backend_for_opencode_cli_returns_opencode_cli_backend(self):
        instance = backend_for("opencode-cli")
        assert isinstance(instance, OpenCodeCliBackend)


# ---------------------------------------------------------------------------
# 14 — _run: error handling
# ---------------------------------------------------------------------------

class TestOpenCodeRun:
    def test_open_cli_error_on_nonzero_exit_with_empty_stdout(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        proc = MagicMock()
        proc.stdout = b""
        proc.stderr = b"something went wrong"
        proc.returncode = 1
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=proc):
            with pytest.raises(OpenCodeCliError, match="opencode CLI failed"):
                backend._run(["opencode"])

    def test_session_expired_error_on_session_not_found(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        proc = MagicMock()
        proc.stdout = b""
        proc.stderr = b"Error: session not found"
        proc.returncode = 1
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=proc):
            with pytest.raises(OpenCodeSessionExpiredError, match="OpenCode session expired"):
                backend._run(["opencode"])

    def test_returns_stdout_on_success(self):
        backend = OpenCodeCliBackend(timeout=5.0)
        proc = MagicMock()
        proc.stdout = b"hello world"
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.opencode_cli.subprocess.run", return_value=proc):
            result = backend._run(["opencode"])
        assert result == "hello world"
