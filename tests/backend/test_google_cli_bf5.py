"""Phase BF-5 additional tests for GoogleCliBackend.

Covers error-handling and edge-case paths in the agy subprocess backend:
  - GoogleCliError and GoogleCliSessionExpiredError exception hierarchy
  - _run: TimeoutExpired → AgentTimeout
  - _run: nonzero returncode with empty stdout (generic + session-expired)
  - _run: plain-text stdout with returncode 0 → {"response": text}
  - _run: stderr logged at WARNING on nonzero exit, DEBUG on success
  - _extract_conversation_id: parses conversation UUID from agy log
  - _parse_with_nudge: pass-through, nudge-retry (--conversation flag), no-infinite-retry,
    non-sentinel ProtocolError re-raise
  - start_session: system prompt included in -p argument, uses --log-file for ID extraction
  - start_session: external_id set from log; None when extraction fails
  - send_message: uses --conversation flag, TypeError on wrong handle, RuntimeError on None external_id
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import uuid as uuid_mod
from unittest.mock import MagicMock, patch

import pytest

from jsa.agents.base import AgentTimeout

from jsa.agents.claude_cli import ClaudeSessionHandle
from jsa.agents.google_cli import (
    GoogleCliBackend,
    GoogleCliError,
    GoogleCliSessionExpiredError,
    GoogleSessionHandle,
)
from jsa.agents.protocol import ProtocolError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FINAL_TEXT = "<<<FINAL>>>\nmy cv\n<<<END>>>\n"


def _make_google_proc(
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
    """GoogleCliError and GoogleCliSessionExpiredError are importable and correctly typed."""

    def test_google_cli_error_is_importable(self):
        from jsa.agents.google_cli import GoogleCliError as GCE
        assert GCE is GoogleCliError

    def test_google_session_expired_error_is_importable(self):
        from jsa.agents.google_cli import GoogleCliSessionExpiredError as GSEE
        assert GSEE is GoogleCliSessionExpiredError

    def test_google_cli_error_is_runtime_error_subclass(self):
        assert issubclass(GoogleCliError, RuntimeError)

    def test_google_session_expired_error_is_google_cli_error_subclass(self):
        assert issubclass(GoogleCliSessionExpiredError, GoogleCliError)

    def test_google_session_expired_error_is_runtime_error_subclass(self):
        assert issubclass(GoogleCliSessionExpiredError, RuntimeError)

    def test_google_cli_error_can_be_raised_and_caught(self):
        with pytest.raises(GoogleCliError):
            raise GoogleCliError("subprocess failed")

    def test_google_session_expired_error_caught_as_google_cli_error(self):
        with pytest.raises(GoogleCliError):
            raise GoogleCliSessionExpiredError("session gone")


# ---------------------------------------------------------------------------
# 2. _run: raises GoogleCliError on nonzero returncode with empty stdout
# ---------------------------------------------------------------------------

class TestRunRaisesOnEmptyStdout:
    """_run raises GoogleCliError when returncode != 0 and stdout is empty."""

    def test_raises_google_cli_error_on_nonzero_returncode_empty_stdout(self):
        proc = _make_empty_proc(returncode=1, stderr=b"some error")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError):
                backend._run(["agy", "-p", "hello"], "ctx")

    def test_raises_google_cli_error_not_expired_on_generic_error(self):
        proc = _make_empty_proc(returncode=1, stderr=b"some generic error")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError) as exc_info:
                backend._run(["agy", "-p", "hello"], "ctx")
        assert type(exc_info.value) is GoogleCliError

    def test_error_message_includes_exit_code(self):
        proc = _make_empty_proc(returncode=2, stderr=b"bad exit")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError, match="2"):
                backend._run(["agy", "-p", "hello"])

    def test_raises_even_when_no_stderr(self):
        proc = _make_empty_proc(returncode=1, stderr=b"")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError, match="no stderr"):
                backend._run(["agy", "-p", "hello"])


# ---------------------------------------------------------------------------
# 3. _run: raises GoogleCliSessionExpiredError on session-not-found stderr
# ---------------------------------------------------------------------------

class TestRunRaisesSessionExpiredError:
    """_run raises GoogleCliSessionExpiredError when stderr contains 'conversation not found'."""

    def test_raises_session_expired_error_on_conversation_not_found(self):
        proc = _make_empty_proc(returncode=1, stderr=b"conversation not found")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliSessionExpiredError):
                backend._run(["agy", "--conversation", "old-id", "-p", "hi"], "old-id")

    def test_session_expired_error_is_also_google_cli_error(self):
        proc = _make_empty_proc(returncode=1, stderr=b"conversation not found")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError):
                backend._run(["agy", "--conversation", "old-id", "-p", "hi"])

    def test_check_is_case_insensitive(self):
        """The implementation uses .lower() to check for 'conversation' and 'not found'."""
        proc = _make_empty_proc(returncode=1, stderr=b"Conversation Not Found for given ID")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliSessionExpiredError):
                backend._run(["agy", "--conversation", "x", "-p", "hi"])

    def test_session_keyword_alone_does_not_trigger_expired(self):
        """'session' alone without 'not found' should give GoogleCliError, not SessionExpired."""
        proc = _make_empty_proc(returncode=1, stderr=b"invalid session configuration")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(GoogleCliError) as exc_info:
                backend._run(["agy", "-p", "hi"])
        assert type(exc_info.value) is GoogleCliError


# ---------------------------------------------------------------------------
# 4. _run: plain-text stdout is accepted and returned as {"response": text}
# ---------------------------------------------------------------------------

class TestRunReturnsPlainTextResponse:
    """_run wraps plain-text agy stdout into {"response": text, "session_id": None}."""

    def test_plain_text_stdout_returned_as_response(self):
        proc = _make_google_proc(response="Hello from agy")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            result = backend._run(["agy", "-p", "hello"])
        assert result["response"] == "Hello from agy"

    def test_session_id_is_none_when_no_log_path(self):
        proc = _make_google_proc(response=_FINAL_TEXT)
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            result = backend._run(["agy", "-p", "hello"])
        assert result["session_id"] is None

    def test_multiline_response_preserved(self):
        multi = "line one\nline two\nline three"
        proc = _make_google_proc(response=multi)
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            result = backend._run(["agy", "-p", "hello"])
        assert "line one" in result["response"]
        assert "line three" in result["response"]

    def test_nonzero_returncode_but_nonempty_stdout_still_returns(self):
        """If returncode != 0 but stdout has content, _run returns it (not raises)."""
        proc = MagicMock()
        proc.stdout = b"partial response"
        proc.stderr = b"some warning"
        proc.returncode = 1
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            result = backend._run(["agy", "-p", "hello"])
        assert result["response"] == "partial response"


# ---------------------------------------------------------------------------
# 5. _extract_conversation_id: parses UUID from agy log file
# ---------------------------------------------------------------------------

class TestExtractConversationId:
    """_extract_conversation_id reads the agy log file for 'Created conversation <uuid>'."""

    def test_returns_uuid_from_log(self, tmp_path):
        log = tmp_path / "agy.log"
        uuid_str = "f39d171d-462e-4eec-a4a3-d4aadc81758b"
        log.write_text(
            f"I0618 22:04:55.186391 46056 server.go:788] Created conversation {uuid_str}\n"
        )
        backend = GoogleCliBackend()
        assert backend._extract_conversation_id(str(log)) == uuid_str

    def test_returns_none_when_log_missing(self, tmp_path):
        backend = GoogleCliBackend()
        assert backend._extract_conversation_id(str(tmp_path / "missing.log")) is None

    def test_returns_none_when_no_conversation_line(self, tmp_path):
        log = tmp_path / "agy.log"
        log.write_text("I0618 server started\nI0618 auth succeeded\n")
        backend = GoogleCliBackend()
        assert backend._extract_conversation_id(str(log)) is None

    def test_returns_first_uuid_when_multiple_present(self, tmp_path):
        log = tmp_path / "agy.log"
        first = "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa"
        second = "bbbbbbbb-0000-0000-0000-bbbbbbbbbbbb"
        log.write_text(
            f"] Created conversation {first}\n"
            f"] Created conversation {second}\n"
        )
        backend = GoogleCliBackend()
        assert backend._extract_conversation_id(str(log)) == first


# ---------------------------------------------------------------------------
# 6. _parse_with_nudge: passes through when sentinel is present
# ---------------------------------------------------------------------------

class TestParseWithNudgePassThrough:
    """_parse_with_nudge returns AgentReply immediately when sentinel is present."""

    async def test_final_sentinel_returns_final_kind(self):
        backend = GoogleCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        assert reply.kind == "final"

    async def test_final_sentinel_content_is_trimmed(self):
        backend = GoogleCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        assert reply.content == "hello"

    async def test_need_input_sentinel_returns_needs_input_kind(self):
        backend = GoogleCliBackend(timeout=5.0)
        reply = await backend._parse_with_nudge(
            "sess-id", "<<<NEED_INPUT>>>\nWhat is your name?\n<<<END>>>"
        )
        assert reply.kind == "needs_input"
        assert "name" in reply.question

    async def test_no_subprocess_called_when_sentinel_present(self):
        """No subprocess.run call when the sentinel is already present."""
        with patch("jsa.agents.google_cli.subprocess.run") as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nhello\n<<<END>>>")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 7. _parse_with_nudge: sends nudge when sentinel is missing
# ---------------------------------------------------------------------------

class TestParseWithNudgeRetry:
    """_parse_with_nudge sends a nudge subprocess call when sentinel is absent."""

    async def test_returns_nudge_reply_with_final_kind(self):
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc):
            backend = GoogleCliBackend(timeout=5.0)
            reply = await backend._parse_with_nudge("sess-id", "this has no sentinel")
        assert reply.kind == "final"
        assert reply.content == "nudged"

    async def test_nudge_cmd_uses_conversation_flag(self):
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "--conversation" in cmd
        assert "sess-id" in cmd

    async def test_nudge_cmd_uses_p_flag(self):
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "-p" in cmd

    async def test_nudge_cmd_p_arg_mentions_missing_sentinel(self):
        """The -p argument for the nudge should mention 'missing' or 'sentinel block'."""
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        nudge_text = cmd[p_idx + 1]
        assert "missing" in nudge_text or "sentinel block" in nudge_text

    async def test_nudge_subprocess_called_exactly_once(self):
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        assert mock_run.call_count == 1

    async def test_nudge_cmd_uses_dangerously_skip_permissions(self):
        nudge_proc = _make_google_proc(response="<<<FINAL>>>\nnudged\n<<<END>>>")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend._parse_with_nudge("sess-id", "this has no sentinel")

        cmd = mock_run.call_args.args[0]
        assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# 8. _parse_with_nudge: re-raises non-sentinel ProtocolError without nudging
# ---------------------------------------------------------------------------

class TestParseWithNudgeNoRetryOnUnterminatedBlock:
    """An unterminated block should be re-raised immediately — no nudge subprocess call."""

    async def test_raises_protocol_error_on_unterminated_block(self):
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(ProtocolError, match="unterminated"):
            await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")

    async def test_subprocess_not_called_on_unterminated_block(self):
        with patch("jsa.agents.google_cli.subprocess.run") as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "<<<FINAL>>>\nno end here")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 9. start_session: system prompt is included in the -p argument
# ---------------------------------------------------------------------------

class TestStartSessionSystemPromptInP:
    """start_session must include the system prompt in the combined -p argument."""

    async def test_system_prompt_included_in_p_argument(self):
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("MY SYSTEM PROMPT", "user msg")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "MY SYSTEM PROMPT" in p_value

    async def test_user_message_also_included_in_p_argument(self):
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("sys prompt", "UNIQUE USER MSG")

        cmd = mock_run.call_args.args[0]
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "UNIQUE USER MSG" in p_value

    async def test_both_system_prompt_and_user_msg_in_single_p_argument(self):
        """Both system prompt and user message go into a single combined -p value."""
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("SYS", "USR")

        cmd = mock_run.call_args.args[0]
        assert cmd.count("-p") == 1
        p_idx = cmd.index("-p")
        p_value = cmd[p_idx + 1]
        assert "SYS" in p_value
        assert "USR" in p_value

    async def test_start_session_uses_log_file_flag(self):
        """start_session passes --log-file so the conversation UUID can be extracted."""
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        cmd = mock_run.call_args.args[0]
        assert "--log-file" in cmd

    async def test_start_session_uses_dangerously_skip_permissions(self):
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            await backend.start_session("sys", "user")

        cmd = mock_run.call_args.args[0]
        assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# 10. start_session: external_id sourced from log extraction
# ---------------------------------------------------------------------------

class TestStartSessionConversationId:
    """start_session sets external_id from _extract_conversation_id (the agy log)."""

    async def test_external_id_set_from_log_extraction(self):
        mock_proc = _make_google_proc()
        expected_id = "f39d171d-462e-4eec-a4a3-d4aadc81758b"
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value=expected_id):
            backend = GoogleCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id == expected_id

    async def test_external_id_is_none_when_log_extraction_fails(self):
        """When _extract_conversation_id returns None, external_id is None."""
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value=None):
            backend = GoogleCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        assert handle.external_id is None

    async def test_handle_id_is_valid_uuid(self):
        mock_proc = _make_google_proc()
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc), \
             patch.object(GoogleCliBackend, "_extract_conversation_id", return_value="conv-uuid"):
            backend = GoogleCliBackend(timeout=5.0)
            handle, _ = await backend.start_session("sys", "user")

        uuid_mod.UUID(handle.id)  # raises ValueError if not a valid UUID


# ---------------------------------------------------------------------------
# 11. send_message: raises TypeError on wrong handle type
# ---------------------------------------------------------------------------

class TestSendMessageWrongHandleType:
    """send_message raises TypeError when given a ClaudeSessionHandle."""

    async def test_raises_type_error_on_claude_session_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(TypeError):
            await backend.send_message(handle, "hello")

    async def test_error_message_mentions_google_session_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(TypeError, match="GoogleSessionHandle"):
            await backend.send_message(handle, "hello")

    async def test_no_subprocess_called_on_wrong_handle(self):
        handle = ClaudeSessionHandle(id="h1", external_id="some-id")
        with patch("jsa.agents.google_cli.subprocess.run") as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(TypeError):
                await backend.send_message(handle, "hello")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 12. send_message: raises RuntimeError when external_id is None
# ---------------------------------------------------------------------------

class TestSendMessageExternalIdNone:
    """send_message raises RuntimeError when GoogleSessionHandle.external_id is None."""

    async def test_raises_runtime_error_on_none_external_id(self):
        handle = GoogleSessionHandle(id="h1", external_id=None)
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError):
            await backend.send_message(handle, "hello")

    async def test_error_message_mentions_external_id(self):
        handle = GoogleSessionHandle(id="h1", external_id=None)
        backend = GoogleCliBackend(timeout=5.0)
        with pytest.raises(RuntimeError, match="external_id is None"):
            await backend.send_message(handle, "hello")

    async def test_no_subprocess_called_on_none_external_id(self):
        handle = GoogleSessionHandle(id="h1", external_id=None)
        with patch("jsa.agents.google_cli.subprocess.run") as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(RuntimeError):
                await backend.send_message(handle, "hello")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 13. send_message: uses --conversation flag
# ---------------------------------------------------------------------------

class TestSendMessageConversationFlag:
    """send_message must use --conversation <id> for session resumption."""

    async def test_send_message_uses_conversation_flag(self):
        mock_proc = _make_google_proc()
        handle = GoogleSessionHandle(id="h1", external_id="conv-uuid-123")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend.send_message(handle, "hello")

        cmd = mock_run.call_args.args[0]
        assert "--conversation" in cmd
        assert "conv-uuid-123" in cmd

    async def test_send_message_uses_dangerously_skip_permissions(self):
        mock_proc = _make_google_proc()
        handle = GoogleSessionHandle(id="h1", external_id="conv-uuid-123")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=mock_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            await backend.send_message(handle, "hello")

        cmd = mock_run.call_args.args[0]
        assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# 14. _run: TimeoutExpired → AgentTimeout (direct test on _run)
# ---------------------------------------------------------------------------

class TestRunTimeoutExpired:
    """_run raises AgentTimeout (not subprocess.TimeoutExpired) on timeout."""

    def test_raises_agent_timeout_on_timeout_expired(self):
        with patch(
            "jsa.agents.google_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=5.0),
        ):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout, match="timed out"):
                backend._run(["agy", "-p", "hello"])

    def test_timeout_message_mentions_timeout_seconds(self):
        with patch(
            "jsa.agents.google_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=5.0),
        ):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout, match="5.0"):
                backend._run(["agy", "-p", "hello"])

    def test_agent_timeout_not_timeout_expired_propagated(self):
        """Ensure subprocess.TimeoutExpired is NOT propagated; only AgentTimeout."""
        with patch(
            "jsa.agents.google_cli.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["agy"], timeout=5.0),
        ):
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(AgentTimeout):
                backend._run(["agy", "-p", "hello"])
            try:
                backend._run(["agy", "-p", "hello"])
            except AgentTimeout:
                pass
            except subprocess.TimeoutExpired:
                pytest.fail("subprocess.TimeoutExpired was not wrapped in AgentTimeout")


# ---------------------------------------------------------------------------
# 15. _run: stderr logging — WARNING on nonzero exit, DEBUG on success
# ---------------------------------------------------------------------------

class TestRunStderrLogging:
    """_run logs stderr at WARNING on nonzero exit and at DEBUG on success."""

    def test_warning_logged_on_nonzero_exit_with_stderr(self, caplog):
        proc = _make_empty_proc(returncode=1, stderr=b"something went wrong")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.google_cli"):
                with pytest.raises(GoogleCliError):
                    backend._run(["agy", "-p", "hello"])

        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "google_cli" in r.name
        ]
        assert len(warning_records) >= 1, "Expected at least one WARNING log record"

    def test_warning_message_contains_stderr_content(self, caplog):
        proc = _make_empty_proc(returncode=1, stderr=b"critical process failure")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.google_cli"):
                with pytest.raises(GoogleCliError):
                    backend._run(["agy", "-p", "hello"])

        warning_text = " ".join(
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        )
        assert "critical process failure" in warning_text

    def test_no_warning_logged_on_success(self, caplog):
        proc = _make_google_proc(response=_FINAL_TEXT)
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with caplog.at_level(logging.WARNING, logger="jsa.agents.google_cli"):
                backend._run(["agy", "-p", "hello"])

        warning_records = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "google_cli" in r.name
        ]
        assert len(warning_records) == 0, "No WARNING should be logged on successful exit"

    def test_debug_logged_on_success_with_stderr(self, caplog):
        """Even on success, stderr is logged at DEBUG level."""
        proc = _make_google_proc(response=_FINAL_TEXT, stderr=b"debug info from agy")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=proc):
            backend = GoogleCliBackend(timeout=5.0)
            with caplog.at_level(logging.DEBUG, logger="jsa.agents.google_cli"):
                backend._run(["agy", "-p", "hello"])

        debug_records = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "google_cli" in r.name
        ]
        assert len(debug_records) >= 1, "Expected at least one DEBUG log record on success with stderr"
        debug_text = " ".join(r.getMessage() for r in debug_records)
        assert "debug info from agy" in debug_text


# ---------------------------------------------------------------------------
# 16. _parse_with_nudge: nudge reply also missing sentinel → ProtocolError propagated
# ---------------------------------------------------------------------------

class TestParseWithNudgeNoInfiniteRetry:
    """When the nudge reply is also sentinel-less, ProtocolError propagates. No infinite retry."""

    async def test_protocol_error_raised_when_nudge_reply_also_has_no_sentinel(self):
        nudge_proc = _make_google_proc(response="still no sentinel here")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("sess-id", "no sentinel here")

        assert mock_run.call_count == 1

    async def test_exactly_one_subprocess_call_on_double_failure(self):
        """Guard against infinite retry: subprocess.run is called exactly once (the nudge)."""
        nudge_proc = _make_google_proc(response="still no sentinel")
        with patch("jsa.agents.google_cli.subprocess.run", return_value=nudge_proc) as mock_run:
            backend = GoogleCliBackend(timeout=5.0)
            with pytest.raises(ProtocolError):
                await backend._parse_with_nudge("s", "original missing sentinel")

        assert mock_run.call_count == 1
