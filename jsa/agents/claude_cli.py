"""ClaudeCliBackend: subprocess -p implementation of AgentBackend for the Claude CLI.

Drives `claude` via non-interactive print mode (`-p`):
  - Fresh session:  claude --output-format text --system-prompt <sys> --session-id <uuid> -p <msg>
  - Subsequent msg: claude --output-format text --resume <uuid> -p <msg>

Session state is stored by the Claude CLI daemon/filesystem; JSA only needs to
remember the session UUID (stored in job.session_external_id).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from jsa.agents._subprocess import run_killable
from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError, parse_reply

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root containing .claude/agents/


class ClaudeCliError(RuntimeError):
    """Raised when the claude subprocess exits non-zero."""


class ClaudeSessionExpiredError(ClaudeCliError):
    """Raised when claude reports the session ID is no longer known."""


@dataclass(kw_only=True)
class ClaudeSessionHandle(SessionHandle):
    """Session handle for ClaudeCliBackend — carries the claude CLI session UUID."""
    id: str
    external_id: str | None  # claude --session-id / --resume token


class ClaudeCliBackend(AgentBackend):
    """AgentBackend that drives the `claude` CLI via subprocess in -p (print) mode.

    Each call to start_session or send_message spawns a short-lived subprocess.
    No pty is needed: -p mode is fully non-interactive and streams to stdout.
    end_session is a no-op because the subprocess has already exited.
    """

    name = "claude-cli"
    RESEARCH_TIMEOUT = 300.0  # web search + multiple fetches can exceed the 120s message-turn default

    def __init__(self, model: str = "Sonnet 5", timeout: float = 120.0) -> None:
        self._model = model
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal subprocess runner (killable async subprocess — see
    # jsa/agents/_subprocess.py for why the whole process group is killed)
    # ------------------------------------------------------------------

    async def _run(self, cmd: list[str], context: str = "", cwd: Path | None = None, timeout: float | None = None) -> str:
        """Run a claude CLI command and return its stdout.

        Raises AgentTimeout if the process exceeds the effective timeout.
        Raises ClaudeSessionExpiredError if the subprocess exits non-zero and
        stderr contains "No conversation found".
        Raises ClaudeCliError if the subprocess exits non-zero for any other reason.
        stderr is logged at WARNING but never mixed into the returned string.
        """
        eff_timeout = timeout if timeout is not None else self._timeout
        returncode, stdout_bytes, stderr_bytes = await run_killable(
            cmd,
            timeout=eff_timeout,
            cwd=str(cwd) if cwd is not None else None,
            label="claude CLI",
        )

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if returncode != 0:
            if stderr_text:
                logger.warning("claude CLI stderr (exit %d): %s", returncode, stderr_text)

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        # Log stderr at DEBUG even on success (useful for "no stdin" warnings etc.)
        if stderr_text:
            logger.debug("claude CLI stderr: %s", stderr_text)

        if returncode != 0:
            # A non-zero exit is always a subprocess failure, never a model reply —
            # even when stdout is non-empty. --output-format text writes some CLI-level
            # errors (e.g. "unrecognized model") to stdout rather than stderr, so a
            # stdout-non-empty check here previously let that text through as if it
            # were the agent's answer, producing a misleading downstream
            # "no sentinel block" ProtocolError instead of surfacing the real cause.
            ctx = f" [{context}]" if context else ""
            if "No conversation found" in stderr_text:
                raise ClaudeSessionExpiredError(
                    f"Claude session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            detail = stderr_text or stdout.strip() or "(no output)"
            raise ClaudeCliError(
                f"claude CLI failed (exit {returncode}){ctx}: {detail}"
            )

        return stdout

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _parse_with_nudge(self, session_id: str, raw: str) -> AgentReply:
        """Try parse_reply(raw); on 'no sentinel block' ProtocolError, nudge once.

        If the first parse succeeds, return the result immediately.
        If the reply is missing the sentinel block, log a warning, send a nudge
        via --resume <session_id>, and return parse_reply of the nudge reply
        (propagating on second failure).
        Any other ProtocolError is re-raised immediately without retrying.
        """
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
            # Before nudging, check whether the raw output indicates a usage/rate
            # limit. If so, skip the nudge and surface a clear error immediately.
            raw_lower = raw.lower()
            _LIMIT_KEYWORDS = ("usage limit", "rate limit", "limit reached", "quota")
            if any(kw in raw_lower for kw in _LIMIT_KEYWORDS):
                raise AgentLimitReached(raw[:500])
            logger.warning(
                "_parse_with_nudge: no sentinel block in reply — sending nudge and retrying once (session=%s)",
                session_id,
            )
            nudge = (
                "Your previous response was missing the required sentinel block. "
                "Please restate your response and end it with exactly one of:\n"
                "<<<NEED_INPUT>>>\n<your question>\n<<<END>>>\n"
                "or\n"
                "<<<FINAL>>>\n<your final content>\n<<<END>>>"
            )
            nudge_cmd = [
                "claude",
                "--output-format", "text",
                "--resume", session_id,
                "--tools", "",
                "-p", nudge,
            ]
            raw2 = await self._run(nudge_cmd, session_id)
            return parse_reply(raw2)  # Propagate on second failure

    # ------------------------------------------------------------------
    # AgentBackend interface
    # ------------------------------------------------------------------

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[ClaudeSessionHandle, AgentReply]:
        """Open a fresh claude CLI session and return the handle + first reply.

        Spawns: claude --output-format text --system-prompt <sys>
                        --session-id <uuid> -p <initial_user_msg>
        """
        session_id = str(uuid.uuid4())
        cmd = [
            "claude",
            "--output-format", "text",
            "--model", self._model,
            "--system-prompt", system_prompt,
            "--session-id", session_id,
            "--tools", "",
            "-p", initial_user_msg,
        ]
        raw = await self._run(cmd, session_id)
        handle = ClaudeSessionHandle(id=str(uuid.uuid4()), external_id=session_id)
        reply = await self._parse_with_nudge(session_id, raw)
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> ClaudeSessionHandle:
        """Return a handle ready for send_message.

        With external_id: the Claude CLI holds the session history; no subprocess
        call needed — just return a handle wrapping the UUID.

        Without external_id: this is an unrecoverable situation in the subprocess
        model (unlike the pty era, we have no conversation buffer to replay into).
        Raise RuntimeError so the orchestrator marks the job failed loudly.
        """
        if external_id is not None:
            # Claude session state is persisted by the CLI; nothing to do here.
            return ClaudeSessionHandle(id=str(uuid.uuid4()), external_id=external_id)

        # No external_id → no way to resume; fail loudly rather than silently
        # generating a spurious extra model turn.
        raise RuntimeError(
            "ClaudeCliBackend.restore_session: external_id is None — cannot "
            "resume without a session UUID. This job's session_external_id was "
            "never persisted; mark the job failed and restart from pending."
        )

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Send a message to an existing session using --resume mode.

        Spawns: claude --output-format text --resume <session_id> -p <text>
        """
        if not isinstance(handle, ClaudeSessionHandle):
            raise TypeError(
                f"expected ClaudeSessionHandle, got {type(handle).__name__}"
            )
        if handle.external_id is None:
            raise RuntimeError(
                "ClaudeSessionHandle.external_id is None — cannot send message "
                "without a valid session UUID."
            )
        cmd = [
            "claude",
            "--output-format", "text",
            "--resume", handle.external_id,
            "--tools", "",
            "-p", text,
        ]
        raw = await self._run(cmd, handle.external_id)
        return await self._parse_with_nudge(handle.external_id, raw)

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op: the subprocess has already exited when start_session/send_message returned."""
        pass

    async def run_research(self, agent_name: str, query: str) -> str:
        """One-shot, non-interactive research via a .claude/agents/ subagent.

        Returns the agent's stdout raw — NO sentinel parsing. Runs from the JSA
        project root so `claude` discovers .claude/agents/<agent_name>.md.
        On any failure (ClaudeCliError / AgentTimeout) the caller MUST treat the
        brief as unavailable and fall back to the NONE placeholder — research is
        best-effort and never fails the job.
        """
        cmd = [
            "claude",
            "--agent", agent_name,
            "--output-format", "text",
            "-p", query,
        ]
        return await self._run(
            cmd, f"research:{agent_name}", _PROJECT_ROOT, self.RESEARCH_TIMEOUT
        )
