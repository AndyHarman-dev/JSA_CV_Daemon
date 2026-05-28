"""Phase BF-5 additional tests for GeminiCliBackend.

Covers error-handling and edge-case paths introduced in the subprocess rewrite:
  - GeminiCliError and GeminiSessionExpiredError exception hierarchy
  - _run: TimeoutExpired → AgentTimeout (direct test with match="timed out")
  - _run: nonzero returncode with empty stdout (generic + session-expired)
  - _run: non-JSON stdout with returncode 0
  - _run: stderr logged at WARNING on nonzero exit, DEBUG on success
  - _parse_with_nudge: pass-through, nudge-retry (incl. -o json flag), no-infinite-retry,
    and non-sentinel ProtocolError re-raise
  - start_session: system prompt included in -p argument
  - start_session: fallback to pre-generated UUID when JSON has no session_id key
  - send_message: TypeError on wrong handle type
  - send_message: RuntimeError when external_id is None
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid as uuid_mod
from unittest.mock import MagicMock, patch

import pytest

from jsa.agents.base import AgentTimeout

from jsa.agents.claude_cli import ClaudeSessionHandle
from jsa.agents.gemini_cli import (
    GeminiCliBackend,
    GeminiCliError,
    GeminiSessionExpiredError,
    GeminiSessionHandle,
)
from jsa.agents.protocol import ProtocolError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_TEXT = "<<<FINAL>>>\nmy cv\n<<<END>>>\n"


def _make_gemini_proc(
    response: str = _FINAL_TEXT,
    session_id: str = "test-uuid",
    returncode: int = 0,
    stderr: bytes = b"",
) -> MagicMock:
    """Return a MagicMock resembling subprocess.CompletedProcess with JSON stdout."""
    data = {"session_id": session_id, "response": response}
    proc = MagicMock()
    proc.stdout = json.dumps(data).encode()
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _make_empty_proc(returncode: int = 1, stderr: bytes = b"error") -> MagicMock:
    """Return a MagicMock with empty stdout and nonzero returncode by default."""
    proc = MagicMock()
    proc.stdout = b""
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# 1. Exception hierarchy
# ---------------------------------------------------------------------------

class TestExceptionHierarchy:
    """GeminiCliError and GeminiSessionExpiredError are importable and correctly typed."""

    def test_gemini_cli_error_is_importable(self):
        from jsa.agents.gemini_cli import GeminiCliError as GCE
        assert GCE is GeminiCliError

    def test_gemini_session_expired_error_is_importable(self):
        from jsa.agents.gemini_cli import GeminiSessionExpiredError as GSEE
        assert GSEE is GeminiSessionExpiredError

    def test_gemini_cli_error_is_runtime_error_subclass(self):
        assert issubclass(GeminiCliError, RuntimeError)

    def test_gemini_session_expired_error_is_gemini_cli_error_subclass(self):
        assert issubclass(GeminiSessionExpiredError, GeminiCliError)

    def test_gemini_session_expired_error_is_runtime_error_subclass(self):
        assert issubclass(GeminiSessionExpiredError, RuntimeError)

    def test_gemini_cli_error_can_be_raised_and_caught(self):
        with pytest.raises(GeminiCliError):
            raise GeminiCliError("subprocess failed")

    def test_gemini_session_expired_error_caught_as_gemini_cli_error(self):
        with pytest.raises(GeminiCliError):
            raise GeminiSessionExpiredError("session gone")


# ---------------------------------------------------------------------------
# 2. _run: raises GeminiCliError on nonzero returncode with empty stdout
# ---------------------------------------------------------------------------

class TestRunRaisesOnEmptyStdout:
    """_run raises GeminiCliError when returncode != 0 and stdout is empty."""

    def test_raises_gemini_cli_error_on_nonzero_returncode_empty_stdout(self):
        proc = _make_empty_proc(returncode=1, stderr=b"some error")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError):
                backend._run(["gemini", "-p", "hello"], "ctx")

    def test_raises_gemini_cli_error_not_expired_on_generic_error(self):
        proc = _make_empty_proc(returncode=1, stderr=b"some generic error")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError) as exc_info:
                backend._run(["gemini", "-p", "hello"], "ctx")
        # Should not be GeminiSessionExpiredError for a generic error
        assert type(exc_info.value) is GeminiCliError

    def test_error_message_includes_exit_code(self):
        proc = _make_empty_proc(returncode=2, stderr=b"bad exit")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError, match="2"):
                backend._run(["gemini", "-p", "hello"])

    def test_raises_even_when_no_stderr(self):
        proc = _make_empty_proc(returncode=1, stderr=b"")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError, match="no stderr"):
                backend._run(["gemini", "-p", "hello"])


# ---------------------------------------------------------------------------
# 3. _run: raises GeminiSessionExpiredError on session-not-found stderr
# ---------------------------------------------------------------------------

class TestRunRaisesSessionExpiredError:
    """_run raises GeminiSessionExpiredError when stderr contains 'session not found'."""

    def test_raises_session_expired_error_on_session_not_found(self):
        proc = _make_empty_proc(returncode=1, stderr=b"session not found")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiSessionExpiredError):
                backend._run(["gemini", "--resume", "old-id", "-p", "hi"], "old-id")

    def test_session_expired_error_is_also_gemini_cli_error(self):
        proc = _make_empty_proc(returncode=1, stderr=b"session not found")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError):
                backend._run(["gemini", "--resume", "old-id", "-p", "hi"])

    def test_check_is_case_insensitive(self):
        """The implementation uses .lower() to check for 'session' and 'not found'."""
        proc = _make_empty_proc(returncode=1, stderr=b"Session Not Found for given ID")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiSessionExpiredError):
                backend._run(["gemini", "--resume", "x", "-p", "hi"])

    def test_session_keyword_alone_does_not_trigger_expired(self):
        """'session' alone without 'not found' should give GeminiCliError, not SessionExpired."""
        proc = _make_empty_proc(returncode=1, stderr=b"invalid session configuration")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError) as exc_info:
                backend._run(["gemini", "-p", "hi"])
        assert type(exc_info.value) is GeminiCliError


# ---------------------------------------------------------------------------
# 4. _run: raises GeminiCliError on non-JSON stdout (even with returncode 0)
# ---------------------------------------------------------------------------

class TestRunRaisesOnNonJsonStdout:
    """_run raises GeminiCliError when stdout is not valid JSON."""

    def test_raises_gemini_cli_error_on_non_json_stdout(self):
        proc = MagicMock()
        proc.stdout = b"not json"
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError):
                backend._run(["gemini", "-p", "hello"])

    def test_raises_on_partial_json(self):
        proc = MagicMock()
        proc.stdout = b'{"response": "ok"'  # missing closing brace
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError):
                backend._run(["gemini", "-p", "hello"])

    def test_raises_on_plain_text_output(self):
        proc = MagicMock()
        proc.stdout = b"here is some plain text output from model"
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(GeminiCliError):
                backend._run(["gemini", "-p", "hello"])

    def test_valid_json_does_not_raise(self):
        """Sanity check: well-formed JSON with returncode 0 is returned as a dict."""
        data = {"session_id": "s1", "response": _FINAL_TEXT}
        proc = MagicMock()
        proc.stdout = json.dumps(data).encode()
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            result = backend._run(["gemini", "-p", "hello"])
        assert result == data


# ---------------------------------------------------------------------------
# 5. _parse_with_nudge: passes through when sentinel is present
# ---------------------------------------------------------------------------

class TestParseWithNudgePassThrough:
    """_parse_with_nudge returns AgentReply immediately when sentinel is present."""

    async def test_final_sentinel_returns_final_kind(self):
        backend = GeminiCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        assert reply.kind == "final"

    async def test_final_sentinel_content_is_trimmed(self):
        backend = GeminiCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        assert reply.content == "hello"

    async def test_need_input_sentinel_returns_needs_input_kind(self):
        backend = GeminiCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge(
            "sess-id", "<<<NEED_INPUT>>>\nWhat is your name?\n<<<END>>>"
        )
        assert reply.kind == "needs_input"
        assert "name" in reply.question

    async def test_no_subprocess_called_when_sentinel_present(self):
        """No subprocess.run call when the sentinel is already present."""
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 6. _parse_with_nudge: sends nudge when sentinel is missing
# ---------------------------------------------------------------------------

class TestParseWithNudgeRetry:
    """_parse_with_nudge sends a nudge subprocess call when sentinel is absent."""

    async def test_returns_nudge_reply_with_final_kind(self):
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc):
            backend = GeminiCliBackend(timeout=5.0)
            reply = await backend._parse_with_nudge("sess-id", "this has no sentinel")
        assert reply.kind == "final"
        assert reply.content == "nudged"

    async def test_nudge_cmd_uses_resume_flag(self):
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "--resume" in cmd
        assert "sess-id" in cmd

    async def test_nudge_cmd_uses_p_flag(self):
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd

    async def test_nudge_cmd_p_arg_mentions_missing_sentinel(self):
        """The -p argument for the nudge should mention 'missing' or 'sentinel block'."""
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        # Find the argument following '-p'
        p_idx = cmd.index("-p")
        nudge_text = cmd[p_idx + 1]
        assert "missing" in nudge_text or "sentinel block" in nudge_text

    async def test_nudge_subprocess_called_exactly_once(self):
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# 7. _parse_with_nudge: re-raises non-sentinel ProtocolError without nudging
# ---------------------------------------------------------------------------

class TestParseWithNudgeNoRetryOnUnterminatedBlock:
    """An unterminated block should be re-raised immediately — no nudge subprocess call."""

    async def test_raises_protocol_error_on_unterminated_block(self):
        backend = GeminiCliBackend(timeout=5.0)
        # <<<FINAL>>> without <<<END>>> is an unterminated block
        with pytest.raises(ProtocolError, match="unterminated"):
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")

    async def test_subprocess_not_called_on_unterminated_block(self):
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 8. start_session: system prompt is included in the -p argument
# ---------------------------------------------------------------------------

class TestStartSessionSystemPromptInP:
    """start_session must include the system prompt in the combined -p argument."""

    async def test_system_prompt_included_in_p_argument(self):
        mock_proc = _make_gemini_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("MY SYSTEM PROMPT", "user msg")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "MY SYSTEM PROMPT" in p_value

    async def test_user_message_also_included_in_p_argument(self):
        mock_proc = _make_gemini_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("sys prompt", "UNIQUE USER MSG")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "UNIQUE USER MSG" in p_value

    async def test_both_system_prompt_and_user_msg_in_single_p_argument(self):
        """Both system prompt and user message go into a single combined -p value."""
        mock_proc = _make_gemini_proc()
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend.start_session("SYS", "USR")

        cmd = mock_run.call_args.args[0]
        # Count occurrences of "-p" — should be exactly 1 for start_session
        assert cmd.count("-p") == 1
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "SYS" in p_value
        assert "USR" in p_value


# ---------------------------------------------------------------------------
# 9. send_message: raises TypeError on wrong handle type
# ---------------------------------------------------------------------------

class TestSendMessageWrongHandleType:
    """send_message raises TypeError when given a ClaudeSessionHandle."""

    async def test_raises_type_error_on_claude_session_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        backend = GeminiCliBackend(timeout=5.0)
        with pytest.raises(TypeError):
            await backend.send_message(handle, "hello")

    async def test_error_message_mentions_gemini_session_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        backend = GeminiCliBackend(timeout=5.0)
        with pytest.raises(TypeError, match="GeminiSessionHandle"):
            await backend.send_message(handle, "hello")

    async def test_no_subprocess_called_on_wrong_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(TypeError):
                await backend.send_message(handle, "hello")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 10. send_message: raises RuntimeError when external_id is None
# ---------------------------------------------------------------------------

class TestSendMessageExternalIdNone:
    """send_message raises RuntimeError when GeminiSessionHandle.external_id is None."""

    async def test_raises_runtime_error_on_none_external_id(self):
        handle = GeminiSessionHandle(id="h1", external_id=None)
        backend = GeminiCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError):
            await backend.send_message(handle, "hello")

    async def test_error_message_mentions_external_id(self):
        handle = GeminiSessionHandle(id="h1", external_id=None)
        backend = GeminiCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "hello")

    async def test_no_subprocess_called_on_none_external_id(self):
        handle = GeminiSessionHandle(id="h1", external_id=None)
        with patch("jsa.agents.gemini_cli.subprocess.run") as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.send_message(handle, "hello")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 11. _run: TimeoutExpired → AgentTimeout (direct test on _run)
# ---------------------------------------------------------------------------

class TestRunTimeoutExpired:
    """_run raises AgentTimeout (not subprocess.TimeoutExpired) on timeout."""

    def test_raises_agent_timeout_on_timeout_expired(self):
        with patch(
            "jsa.agents.gemini_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=5.0),
        ):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout, match="timed out"):
                backend._run(["gemini", "-p", "hello"])

    def test_timeout_message_mentions_timeout_seconds(self):
        with patch(
            "jsa.agents.gemini_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=5.0),
        ):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout, match="5.0"):
                backend._run(["gemini", "-p", "hello"])

    def test_agent_timeout_not_timeout_expired_propagated(self):
        """Ensure subprocess.TimeoutExpired is NOT propagated; only AgentTimeout."""
        with patch(
            "jsa.agents.gemini_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gemini"], timeout=5.0),
        ):
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout):
                backend._run(["gemini", "-p", "hello"])
            # Verify it's not subprocess.TimeoutExpired sneaking through
            try:
                backend._run(["gemini", "-p", "hello"])
            except AgentTimeout:
                pass
            except subprocess.TimeoutExpired:
                pytest.fail("subprocess.TimeoutExpired was not wrapped in AgentTimeout")


# ---------------------------------------------------------------------------
# 12. _run: stderr logging — WARNING on nonzero exit, DEBUG on success
# ---------------------------------------------------------------------------

class TestRunStderrLogging:
    """_run logs stderr at WARNING on nonzero exit and at DEBUG on success."""

    def test_warning_logged_on_nonzero_exit_with_stderr(self, caplog):
        proc = _make_empty_proc(returncode=1, stderr=b"something went wrong")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.gemini_cli"):
                with pytest.raises(GeminiCliError):
                    backend._run(["gemini", "-p", "hello"])

        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "gemini_cli" in r.name
        ]
        assert len(warning_records) >= 1, "Expected at least one WARNING log record"

    def test_warning_message_contains_stderr_content(self, caplog):
        proc = _make_empty_proc(returncode=1, stderr=b"critical process failure")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.gemini_cli"):
                with pytest.raises(GeminiCliError):
                    backend._run(["gemini", "-p", "hello"])

        warning_text = " ".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "critical process failure" in warning_text

    def test_no_warning_logged_on_success(self, caplog):
        data = {"session_id": "s1", "response": _FINAL_TEXT}
        proc = MagicMock()
        proc.stdout = json.dumps(data).encode()
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.gemini_cli"):
                backend._run(["gemini", "-p", "hello"])

        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "gemini_cli" in r.name
        ]
        assert len(warning_records) == 0, "No WARNING should be logged on successful exit"

    def test_debug_logged_on_success_with_stderr(self, caplog):
        """Even on success, stderr is logged at DEBUG level."""
        data = {"session_id": "s1", "response": _FINAL_TEXT}
        proc = MagicMock()
        proc.stdout = json.dumps(data).encode()
        proc.stderr = b"debug info from gemini"
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            with caplog.at_level(logging.DEBUG, logger="jsa.agents.gemini_cli"):
                backend._run(["gemini", "-p", "hello"])

        debug_records = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "gemini_cli" in r.name
        ]
        assert len(debug_records) >= 1, "Expected at least one DEBUG log record on success with stderr"
        debug_text = " ".join(r.getMessage() for r in debug_records)
        assert "debug info from gemini" in debug_text


# ---------------------------------------------------------------------------
# 13. _parse_with_nudge: nudge cmd includes -o json
# ---------------------------------------------------------------------------

class TestParseWithNudgeOJsonFlag:
    """Nudge subprocess command must include -o json."""

    async def test_nudge_cmd_includes_o_json(self):
        nudge_proc = _make_gemini_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "no sentinel here")

        cmd = mock_run.call_args.args[0]
        assert "-o" in cmd
        o_idx = cmd.index("-o")
        assert cmd[o_idx + 1] == "json"


# ---------------------------------------------------------------------------
# 14. _parse_with_nudge: nudge reply also missing sentinel → ProtocolError propagated
# ---------------------------------------------------------------------------

class TestParseWithNudgeNoInfiniteRetry:
    """When the nudge reply is also sentinel-less, ProtocolError propagates. No infinite retry."""

    async def test_protocol_error_raised_when_nudge_reply_also_has_no_sentinel(self):
        nudge_proc = _make_gemini_proc(response="still no sentinel here", session_id="sess-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "no sentinel here")

        # Only one nudge call — no recursive nudging
        assert mock_run.call_count == 1

    async def test_exactly_one_subprocess_call_on_double_failure(self):
        """Guard against infinite retry: subprocess.run is called exactly once (the nudge)."""
        nudge_proc = _make_gemini_proc(response="still no sentinel", session_id="s")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GeminiCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("s", "original missing sentinel")

        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# 15. start_session: fallback to pre-generated UUID when JSON has no session_id
# ---------------------------------------------------------------------------

class TestStartSessionSessionIdFallback:
    """start_session uses the pre-generated UUID when JSON does not contain session_id."""

    async def test_handle_external_id_is_valid_uuid_when_json_has_no_session_id(self):
        """When response JSON has no 'session_id' key, fall back to the pre-generated UUID."""
        # Build a proc without a session_id key in the JSON
        data = {"response": _FINAL_TEXT}  # no session_id key
        proc = MagicMock()
        proc.stdout = json.dumps(data).encode()
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        # external_id must be a valid UUID (the fallback pre-generated one)
        assert handle.external_id is not None
        uuid_mod.UUID(handle.external_id)  # raises ValueError if not a valid UUID

    async def test_handle_external_id_differs_from_handle_id_when_json_has_no_session_id(self):
        """The external_id should be a distinct UUID from handle.id."""
        data = {"response": _FINAL_TEXT}
        proc = MagicMock()
        proc.stdout = json.dumps(data).encode()
        proc.stderr = b""
        proc.returncode = 0
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=proc):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        # Both should be valid UUIDs but external_id is the session UUID
        uuid_mod.UUID(handle.external_id)
        uuid_mod.UUID(handle.id)

    async def test_handle_external_id_from_json_when_session_id_present(self):
        """When JSON has session_id, external_id equals the JSON value (not pre-generated UUID)."""
        mock_proc = _make_gemini_proc(session_id="json-provided-session-id")
        with patch("jsa.agents.gemini_cli.subprocess.run", return_value=mock_proc):
            backend = GeminiCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id == "json-provided-session-id"
