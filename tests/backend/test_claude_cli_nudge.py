"""Tests for ClaudeCliBackend._parse_with_nudge (Phase BF-4).

Strategy
--------
- Tests 1-4 call `_parse_with_nudge` directly with a pre-formed `raw` string.
  In these tests the initial subprocess call that produces `raw` has already
  happened (conceptually), so `_run` call counts refer only to nudge calls:
    - happy path: 0 nudge calls → `_run` called 0 times
    - missing-sentinel nudge succeeds: 1 nudge call → `_run` called 1 time
    - missing-sentinel nudge also fails: 1 nudge call → `_run` called 1 time
    - non-"no sentinel block" ProtocolError: 0 nudge calls → `_run` called 0 times

  Counts in the section comments reflect this (the spec says "exactly once /
  twice" counting the initial -p call through start_session/send_message, not
  through the direct helper invocation used in tests 1-4).

- Tests 5-6 route through `start_session` and `send_message`, so `_run` counts
  include the initial call, matching the spec verbatim.

No unittest.mock or MagicMock is used. Instead, a thin subclass overrides `_run`
with a scripted-reply queue and an invocation counter.
"""

from __future__ import annotations

import pytest

from jsa.agents.base import AgentReply
from jsa.agents.claude_cli import (
    ClaudeCliBackend,
    ClaudeCliError,
    ClaudeSessionExpiredError,
    ClaudeSessionHandle,
)
from jsa.agents.protocol import ProtocolError


# ---------------------------------------------------------------------------
# Test double: subclass of ClaudeCliBackend with scripted _run responses
# ---------------------------------------------------------------------------

class ScriptedClaudeBackend(ClaudeCliBackend):
    """ClaudeCliBackend subclass that returns scripted strings from _run.

    `_run_responses` is a list of strings returned in order. Each call to
    _run pops the next entry. An IndexError is raised if the list is exhausted.
    `run_call_count` tracks how many times _run was invoked.
    `run_call_args` records the cmd argument for each call.
    `run_call_stdin` records the stdin kwarg for each call (U5: prompt payload
    is now delivered via stdin, not as a trailing argv element — see
    jsa/agents/claude_cli.py).
    """

    def __init__(self, run_responses: list[str]) -> None:
        super().__init__(timeout=5.0)
        self._run_responses: list[str] = list(run_responses)
        self.run_call_count: int = 0
        self.run_call_args: list[list[str]] = []
        self.run_call_stdin: list[str | None] = []

    # _run must be async: ClaudeCliBackend calls it via `await self._run(cmd)`
    # (jsa/agents/_subprocess.py's killable async subprocess seam).
    async def _run(
        self,
        cmd: list[str],
        context: str = "",
        cwd=None,
        timeout: float | None = None,
        *,
        stdin: str | None = None,
    ) -> str:
        self.run_call_count += 1
        self.run_call_args.append(cmd)
        self.run_call_stdin.append(stdin)
        if not self._run_responses:
            raise IndexError("ScriptedClaudeBackend: no more scripted _run responses")
        return self._run_responses.pop(0)


# ---------------------------------------------------------------------------
# Convenience sentinel strings
# ---------------------------------------------------------------------------

VALID_FINAL = "<<<FINAL>>>\nsome content\n<<<END>>>"
VALID_NEED_INPUT = "<<<NEED_INPUT>>>\nsome question\n<<<END>>>"
BARE_TEXT = "This is a plain reply with no sentinel at all."
UNCLOSED_FINAL = "<<<FINAL>>>\nsome content"   # no <<<END>>> → "unterminated block"


# ---------------------------------------------------------------------------
# Tests 1-4: _parse_with_nudge directly
# ---------------------------------------------------------------------------

class TestParseWithNudgeDirectHappyPath:
    """Test 1 — first reply has a valid sentinel; no nudge branch taken."""

    async def test_returns_agent_reply(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        # Pass raw directly; _run is only called if the nudge branch is entered.
        reply = await backend._parse_with_nudge("sess-abc", VALID_FINAL)
        assert isinstance(reply, AgentReply)

    async def test_kind_is_final(self):
        backend = ScriptedClaudeBackend(run_responses=[])
        reply = await backend._parse_with_nudge("sess-abc", VALID_FINAL)
        assert reply.kind == "final"

    async def test_content_matches(self):
        backend = ScriptedClaudeBackend(run_responses=[])
        reply = await backend._parse_with_nudge("sess-abc", VALID_FINAL)
        assert reply.content == "some content"

    async def test_run_not_called_when_sentinel_present(self):
        """Nudge branch never taken → _run called 0 times (only nudge calls counted here)."""
        backend = ScriptedClaudeBackend(run_responses=[])
        await backend._parse_with_nudge("sess-abc", VALID_FINAL)
        assert backend.run_call_count == 0

    async def test_need_input_sentinel_also_passes_immediately(self):
        backend = ScriptedClaudeBackend(run_responses=[])
        reply = await backend._parse_with_nudge("sess-abc", VALID_NEED_INPUT)
        assert reply.kind == "needs_input"
        assert backend.run_call_count == 0


class TestParseWithNudgeMissingSentinelNudgeSucceeds:
    """Test 2 — first reply missing sentinel; second (nudge) reply has sentinel."""

    async def test_returns_agent_reply_from_nudge(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        reply = await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert isinstance(reply, AgentReply)

    async def test_reply_reflects_nudge_content(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        reply = await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert reply.kind == "final"
        assert reply.content == "some content"

    async def test_run_called_once_for_nudge(self):
        """Nudge branch taken → _run called exactly once (the nudge subprocess call)."""
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert backend.run_call_count == 1

    async def test_nudge_cmd_contains_resume_flag(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert backend.run_call_count == 1
        nudge_cmd = backend.run_call_args[0]
        assert "--resume" in nudge_cmd
        assert "sess-xyz" in nudge_cmd

    async def test_nudge_cmd_contains_p_flag(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        nudge_cmd = backend.run_call_args[0]
        assert "-p" in nudge_cmd

    async def test_nudge_cmd_contains_no_tools_flag(self):
        """Nudge retry must also disable all tools — same regression guard as the
        original call (a missing sentinel block is no excuse to grant tool access)."""
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        nudge_cmd = backend.run_call_args[0]
        assert "--tools" in nudge_cmd
        assert nudge_cmd[nudge_cmd.index("--tools") + 1] == ""


class TestParseWithNudgeMissingSentinelBothFail:
    """Test 3 — both first and nudge reply missing sentinel → ProtocolError propagated."""

    async def test_raises_protocol_error(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT])
        with pytest.raises(ProtocolError):
            await backend._parse_with_nudge("sess-xyz", BARE_TEXT)

    async def test_error_message_indicates_no_sentinel(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT])
        with pytest.raises(ProtocolError, match="no sentinel block"):
            await backend._parse_with_nudge("sess-xyz", BARE_TEXT)

    async def test_run_called_once_for_nudge_attempt(self):
        """Nudge was attempted (first parse failed) → _run called exactly once."""
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT])
        with pytest.raises(ProtocolError):
            await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert backend.run_call_count == 1


class TestParseWithNudgeNonNoSentinelProtocolError:
    """Test 4 — first reply has a different ProtocolError (unclosed sentinel).

    The discriminator in _parse_with_nudge is:
        if "no sentinel block" not in str(exc): raise
    An unclosed `<<<FINAL>>>` raises ProtocolError("unterminated block"), which
    does not contain "no sentinel block" → must be re-raised immediately without
    calling _run (no nudge attempt).
    """

    async def test_raises_protocol_error_immediately(self):
        backend = ScriptedClaudeBackend(run_responses=[])
        with pytest.raises(ProtocolError):
            await backend._parse_with_nudge("sess-xyz", UNCLOSED_FINAL)

    async def test_error_message_indicates_unterminated(self):
        backend = ScriptedClaudeBackend(run_responses=[])
        with pytest.raises(ProtocolError, match="unterminated block"):
            await backend._parse_with_nudge("sess-xyz", UNCLOSED_FINAL)

    async def test_run_never_called(self):
        """Non-'no sentinel block' error → no nudge branch → _run called 0 times."""
        backend = ScriptedClaudeBackend(run_responses=[])
        with pytest.raises(ProtocolError):
            await backend._parse_with_nudge("sess-xyz", UNCLOSED_FINAL)
        assert backend.run_call_count == 0

    async def test_non_nudge_error_is_not_missing_sentinel(self):
        """Confirm the error is NOT the 'no sentinel block' variant."""
        backend = ScriptedClaudeBackend(run_responses=[])
        with pytest.raises(ProtocolError) as exc_info:
            await backend._parse_with_nudge("sess-xyz", UNCLOSED_FINAL)
        assert "no sentinel block" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tests 5-6: integration through start_session and send_message
# (counts include the initial _run call as per the phase spec)
# ---------------------------------------------------------------------------

class TestStartSessionNudgeIntegration:
    """Test 5 — start_session: first reply missing sentinel, nudge reply has sentinel.

    _run is called exactly twice:
      call 1 — fresh session subprocess (initial -p call)
      call 2 — nudge resume subprocess
    """

    async def test_returns_tuple_of_handle_and_reply(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        result = await backend.start_session("sys prompt", "initial message")
        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_handle_external_id_is_nonempty_string(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        handle, _reply = await backend.start_session("sys prompt", "initial message")
        assert isinstance(handle, ClaudeSessionHandle)
        assert isinstance(handle.external_id, str)
        assert handle.external_id  # non-empty

    async def test_reply_kind_is_final_from_nudge(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        _handle, reply = await backend.start_session("sys prompt", "initial message")
        assert reply.kind == "final"

    async def test_reply_content_from_nudge(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        _handle, reply = await backend.start_session("sys prompt", "initial message")
        assert reply.content == "some content"

    async def test_run_called_exactly_twice(self):
        """Initial call (1) + nudge call (2) = 2 total."""
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.start_session("sys prompt", "initial message")
        assert backend.run_call_count == 2

    async def test_first_run_call_contains_session_id_flag(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.start_session("sys prompt", "initial message")
        first_cmd = backend.run_call_args[0]
        assert "--session-id" in first_cmd

    async def test_second_run_call_contains_resume_flag(self):
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.start_session("sys prompt", "initial message")
        second_cmd = backend.run_call_args[1]
        assert "--resume" in second_cmd


class TestSendMessageNudgeIntegration:
    """Test 6 — send_message: first reply missing sentinel, nudge succeeds.

    _run is called exactly twice:
      call 1 — initial --resume call for the user message
      call 2 — nudge --resume call
    """

    async def test_returns_agent_reply(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        reply = await backend.send_message(handle, "user message")
        assert isinstance(reply, AgentReply)

    async def test_reply_is_from_nudge(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        reply = await backend.send_message(handle, "user message")
        assert reply.kind == "final"
        assert reply.content == "some content"

    async def test_run_called_exactly_twice(self):
        """Initial send (1) + nudge (2) = 2 total."""
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.send_message(handle, "user message")
        assert backend.run_call_count == 2

    async def test_first_run_call_uses_resume_with_session_id(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.send_message(handle, "user message")
        first_cmd = backend.run_call_args[0]
        assert "--resume" in first_cmd
        assert "sess-uuid" in first_cmd

    async def test_second_run_call_also_uses_resume(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[BARE_TEXT, VALID_FINAL])
        await backend.send_message(handle, "user message")
        second_cmd = backend.run_call_args[1]
        assert "--resume" in second_cmd
        assert "sess-uuid" in second_cmd


# ---------------------------------------------------------------------------
# Test double: subclass of ClaudeCliBackend that simulates subprocess failures
# ---------------------------------------------------------------------------

class FailingClaudeBackend(ClaudeCliBackend):
    """Simulates claude CLI subprocess failures without invoking a real subprocess."""

    def __init__(self, returncode: int, stderr: str, stdout: str = "") -> None:
        super().__init__(timeout=30.0)
        self._returncode = returncode
        self._stderr = stderr
        self._stdout = stdout
        self._invocation_count: int = 0

    async def _run(
        self,
        cmd: list[str],
        context: str = "",
        cwd=None,
        timeout: float | None = None,
        *,
        stdin: str | None = None,
    ) -> str:
        self._invocation_count += 1
        # Replicate the exact condition from production _run:
        if self._returncode != 0 and not self._stdout.strip():
            stderr_text = self._stderr
            ctx = f" [{context}]" if context else ""
            if "No conversation found" in stderr_text:
                raise ClaudeSessionExpiredError(
                    f"Claude session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            raise ClaudeCliError(
                f"claude CLI failed (exit {self._returncode}){ctx}: {stderr_text or '(no stderr)'}"
            )
        return self._stdout


# ---------------------------------------------------------------------------
# Tests 7-11: ClaudeCliError / ClaudeSessionExpiredError raising behaviour
# ---------------------------------------------------------------------------

class TestClaudeCliErrorRaising:
    """Test the error-raising paths in _run when the subprocess fails."""

    async def test_run_session_not_found_raises_session_expired(self):
        """Non-zero exit with 'No conversation found' in stderr raises ClaudeSessionExpiredError."""
        backend = FailingClaudeBackend(
            returncode=1,
            stderr="No conversation found with session ID: abc-123",
        )
        handle = ClaudeSessionHandle(id="h1", external_id="sess-handle")
        with pytest.raises(ClaudeSessionExpiredError) as exc_info:
            await backend.send_message(handle, "hello")
        # The stderr text (including "abc-123") appears in the error message.
        assert "abc-123" in str(exc_info.value)

    async def test_run_generic_failure_raises_cli_error(self):
        """Non-zero exit with unrecognised stderr raises ClaudeCliError (not the subclass)."""
        backend = FailingClaudeBackend(
            returncode=1,
            stderr="Some other error",
        )
        handle = ClaudeSessionHandle(id="h2", external_id="sess-handle2")
        with pytest.raises(ClaudeCliError) as exc_info:
            await backend.send_message(handle, "hello")
        # Must NOT be the session-expired subclass.
        assert not isinstance(exc_info.value, ClaudeSessionExpiredError)
        assert "Some other error" in str(exc_info.value)

    async def test_run_nonzero_with_stdout_returns_stdout(self):
        """Non-zero exit but non-empty stdout: no error is raised; stdout is parsed normally."""
        backend = FailingClaudeBackend(
            returncode=1,
            stderr="warning",
            stdout="<<<FINAL>>>\ncontent\n<<<END>>>",
        )
        handle = ClaudeSessionHandle(id="h3", external_id="sess-handle3")
        # Should return an AgentReply (no exception) because stdout is non-empty.
        reply = await backend.send_message(handle, "hello")
        assert isinstance(reply, AgentReply)
        assert reply.kind == "final"

    async def test_session_expired_bypasses_nudge(self):
        """ClaudeSessionExpiredError is raised before _parse_with_nudge can attempt a nudge.

        _run must be called exactly once — the exception propagates immediately,
        so no nudge subprocess call is made.
        """
        backend = FailingClaudeBackend(
            returncode=1,
            stderr="No conversation found with session ID: xyz",
        )
        handle = ClaudeSessionHandle(id="h4", external_id="sess-xyz")
        with pytest.raises(ClaudeSessionExpiredError):
            await backend.send_message(handle, "hello")
        assert backend._invocation_count == 1

    async def test_start_session_session_expired_propagates(self):
        """ClaudeSessionExpiredError raised in start_session propagates to the caller."""
        backend = FailingClaudeBackend(
            returncode=1,
            stderr="No conversation found with session ID: newone",
        )
        with pytest.raises(ClaudeSessionExpiredError):
            await backend.start_session("system prompt", "user msg")


# ---------------------------------------------------------------------------
# Tests 12+: U5 — prompt payload delivered via stdin, never as an argv element
# ---------------------------------------------------------------------------
#
# Bug: start_session/send_message/run_research/_parse_with_nudge's nudge_cmd
# all appended the prompt text as a trailing `-p <text>` argv element. The
# whole argv goes through a single execve() subject to the OS ARG_MAX limit
# (1MB on macOS) — a large paste (base CV JSON, JD, or free-text revision)
# could trip a hard `OSError: [Errno 7] Argument list too long` before the
# model ever saw the prompt. Fix: `-p` is now the terminal cmd element (no
# following value) and the payload is passed via `_run`'s `stdin` kwarg,
# which `run_killable` feeds to the subprocess's stdin instead of argv.
#
# These tests use ScriptedClaudeBackend/FailingClaudeBackend's recording of
# the `stdin` kwarg (see run_call_stdin above) to prove the wiring without a
# real subprocess; test_subprocess_killable.py separately proves the
# underlying run_killable/communicate(input=...) plumbing survives an
# actual >ARG_MAX payload with a real child process.


class TestStartSessionUsesStdinNotArgv:
    async def test_large_initial_msg_not_in_cmd(self):
        """A large user-message payload never appears anywhere in argv."""
        large_payload = "X" * (2 * 1024 * 1024)  # 2MB, over macOS ARG_MAX (1MB)
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.start_session("system prompt", large_payload)
        first_cmd = backend.run_call_args[0]
        assert large_payload not in first_cmd
        assert all(large_payload not in part for part in first_cmd)

    async def test_initial_msg_passed_via_stdin_kwarg(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.start_session("system prompt", "initial message")
        assert backend.run_call_stdin[0] == "initial message"

    async def test_p_flag_is_terminal_cmd_element(self):
        """`-p` must be the last argv element (no trailing value) so the CLI
        falls back to reading the prompt from stdin."""
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.start_session("system prompt", "initial message")
        first_cmd = backend.run_call_args[0]
        assert first_cmd[-1] == "-p"

    async def test_system_prompt_remains_argv_element(self):
        """--system-prompt is documented as staying argv-based (template-sized,
        not data-sized) — this pins that intentional scope boundary."""
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.start_session("a system prompt", "initial message")
        first_cmd = backend.run_call_args[0]
        assert "--system-prompt" in first_cmd
        assert first_cmd[first_cmd.index("--system-prompt") + 1] == "a system prompt"


class TestSendMessageUsesStdinNotArgv:
    async def test_large_text_not_in_cmd(self):
        large_payload = "Y" * (2 * 1024 * 1024)
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.send_message(handle, large_payload)
        first_cmd = backend.run_call_args[0]
        assert all(large_payload not in part for part in first_cmd)

    async def test_text_passed_via_stdin_kwarg(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.send_message(handle, "user message")
        assert backend.run_call_stdin[0] == "user message"

    async def test_p_flag_is_terminal_cmd_element(self):
        handle = ClaudeSessionHandle(id="h1", external_id="sess-uuid")
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        await backend.send_message(handle, "user message")
        first_cmd = backend.run_call_args[0]
        assert first_cmd[-1] == "-p"


class TestNudgeUsesStdinNotArgv:
    async def test_nudge_text_passed_via_stdin_not_argv(self):
        backend = ScriptedClaudeBackend(run_responses=[VALID_FINAL])
        reply = await backend._parse_with_nudge("sess-xyz", BARE_TEXT)
        assert reply.kind == "final"
        nudge_cmd = backend.run_call_args[0]
        assert nudge_cmd[-1] == "-p"
        assert backend.run_call_stdin[0] is not None
        assert "sentinel block" in backend.run_call_stdin[0]


class TestRunResearchUsesStdinNotArgv:
    async def test_large_query_not_in_cmd(self):
        large_query = "Z" * (2 * 1024 * 1024)
        backend = ScriptedClaudeBackend(run_responses=["research output"])
        await backend.run_research("some-agent", large_query)
        first_cmd = backend.run_call_args[0]
        assert all(large_query not in part for part in first_cmd)

    async def test_query_passed_via_stdin_kwarg(self):
        backend = ScriptedClaudeBackend(run_responses=["research output"])
        await backend.run_research("some-agent", "a research query")
        assert backend.run_call_stdin[0] == "a research query"

    async def test_p_flag_is_terminal_cmd_element(self):
        backend = ScriptedClaudeBackend(run_responses=["research output"])
        await backend.run_research("some-agent", "a research query")
        first_cmd = backend.run_call_args[0]
        assert first_cmd[-1] == "-p"
