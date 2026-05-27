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
from jsa.agents.claude_cli import ClaudeCliBackend, ClaudeSessionHandle
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
    """

    def __init__(self, run_responses: list[str]) -> None:
        super().__init__(timeout=5.0)
        self._run_responses: list[str] = list(run_responses)
        self.run_call_count: int = 0
        self.run_call_args: list[list[str]] = []

    # _run must be a regular method (not async) because ClaudeCliBackend calls
    # it via `await asyncio.to_thread(self._run, cmd)`.
    def _run(self, cmd: list[str], context: str = "") -> str:
        self.run_call_count += 1
        self.run_call_args.append(cmd)
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
