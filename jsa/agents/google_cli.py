"""GoogleCliBackend: subprocess -p implementation of AgentBackend for the Google `agy` CLI.

Drives `agy` via non-interactive print mode (`-p`) with plain-text output:
  - Fresh session:  agy --dangerously-skip-permissions -p <sys+msg> --log-file <tmp>
                    → parse conversation UUID from log → store as external_id
  - Subsequent msg: agy --dangerously-skip-permissions --conversation <uuid> -p <msg>

Session state is stored by the agy CLI on disk (~/.gemini/antigravity-cli/conversations/).
JSA only needs to remember the conversation UUID (stored in job.session_external_id).

No pty is required: -p mode is fully non-interactive and the session persists on
disk between invocations. end_session is a no-op because the subprocess has
already exited.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass

from jsa.agents._subprocess import run_killable
from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError, parse_reply

logger = logging.getLogger(__name__)

_CONVERSATION_RE = re.compile(r"Created conversation ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


class GoogleCliError(RuntimeError):
    """Raised when the agy subprocess fails with no usable output."""


class GoogleCliSessionExpiredError(GoogleCliError):
    """Raised when agy reports the conversation ID is no longer known."""


@dataclass(kw_only=True)
class GoogleSessionHandle(SessionHandle):
    """Session handle for GoogleCliBackend — carries the agy conversation UUID."""
    id: str
    external_id: str | None  # agy conversation UUID, used for --conversation


class GoogleCliBackend(AgentBackend):
    """AgentBackend that drives the `agy` CLI via subprocess in -p (print) mode.

    Each call to start_session or send_message spawns a short-lived subprocess.
    No pty is needed: -p mode is fully non-interactive.
    end_session is a no-op because the subprocess has already exited.

    Session continuity: start_session captures the conversation UUID from a
    per-invocation --log-file, then subsequent send_message calls use --conversation <uuid>.
    """

    name = "google-cli"
    RESEARCH_TIMEOUT = 300.0  # web search + multiple fetches can exceed the 120s message-turn default
    # `agy --help` has no output-format/stream/json flag (re-verified live during
    # the agent-chat-upgrade plan's Phase 7) — no token-level channel exists here.
    # Left as the inherited AgentBackend default (False), explicit for clarity;
    # the pipeline shows a plain "working..." indicator for this backend instead.
    supports_streaming = False

    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal subprocess runner (killable async subprocess — see
    # jsa/agents/_subprocess.py for why the whole process group is killed)
    # ------------------------------------------------------------------

    def _extract_conversation_id(self, log_path: str) -> str | None:
        """Scan a per-invocation agy log file for the created conversation UUID."""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = _CONVERSATION_RE.search(line)
                    if m:
                        return m.group(1)
        except OSError:
            pass
        return None

    async def _run(
        self,
        cmd: list[str],
        context: str = "",
        timeout: float | None = None,
        log_path: str | None = None,
    ) -> dict:
        """Run an agy CLI command. Returns {"response": text, "session_id": uuid_or_None}.

        agy outputs plain text to stdout (no -o json equivalent).
        If log_path is provided, the conversation UUID is extracted from it and
        returned as "session_id" so callers can store it for --conversation resumption.

        timeout overrides self._timeout when provided (e.g. for research calls).

        Raises AgentTimeout if the process exceeds the effective timeout.
        Raises GoogleCliSessionExpiredError if stderr suggests the conversation is unknown.
        Raises GoogleCliError if the subprocess exits non-zero with no stdout.
        stderr is logged at WARNING on nonzero exit, at DEBUG otherwise.
        """
        eff_timeout = timeout if timeout is not None else self._timeout
        returncode, stdout_bytes, stderr_bytes = await run_killable(
            cmd,
            timeout=eff_timeout,
            label="agy CLI",
        )

        ctx = f" [{context}]" if context else ""

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if returncode != 0:
            if stderr_text:
                logger.warning("agy CLI stderr (exit %d): %s", returncode, stderr_text)
        else:
            if stderr_text:
                logger.debug("agy CLI stderr: %s", stderr_text)

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()

        if returncode != 0 and not stdout:
            stderr_lower = stderr_text.lower()
            if "conversation" in stderr_lower and "not found" in stderr_lower:
                raise GoogleCliSessionExpiredError(
                    f"Google CLI session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            raise GoogleCliError(
                f"agy CLI failed (exit {returncode}){ctx}: {stderr_text or '(no stderr)'}"
            )

        session_id = await asyncio.to_thread(self._extract_conversation_id, log_path) if log_path else None
        return {"response": stdout, "session_id": session_id}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _parse_with_nudge(self, session_id: str, raw: str) -> AgentReply:
        """Try parse_reply(raw); on 'no sentinel block' ProtocolError, nudge once.

        If the first parse succeeds, return the result immediately.
        If the reply is missing the sentinel block, log a warning, send a nudge
        via --conversation <session_id>, and return parse_reply of the nudge reply
        (propagating on second failure).
        Any other ProtocolError is re-raised immediately without retrying.
        """
        try:
            return parse_reply(raw)
        except ProtocolError as exc:
            if "no sentinel block" not in str(exc):
                raise
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
                "agy",
                "--dangerously-skip-permissions",
                "--conversation", session_id,
                "-p", nudge,
            ]
            nudge_data = await self._run(nudge_cmd, session_id)
            return parse_reply(nudge_data["response"])  # Propagate on second failure

    # ------------------------------------------------------------------
    # AgentBackend interface
    # ------------------------------------------------------------------

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[GoogleSessionHandle, AgentReply]:
        """Open a fresh agy session and return the handle + first reply.

        Spawns: agy --dangerously-skip-permissions --log-file <tmp>
                    -p <system_prompt + initial_user_msg>

        The conversation UUID is extracted from the temp log file and stored
        in handle.external_id for subsequent --conversation resumption.
        """
        log_fd, log_path = tempfile.mkstemp(prefix="agy-jsa-", suffix=".log")
        os.close(log_fd)
        try:
            cmd = [
                "agy",
                "--dangerously-skip-permissions",
                "--log-file", log_path,
                "-p", f"{system_prompt}\n\n{initial_user_msg}",
            ]
            data = await self._run(cmd, "start_session", None, log_path)
        finally:
            try:
                os.unlink(log_path)
            except OSError:
                pass

        actual_session_id = data.get("session_id")
        if actual_session_id is None:
            logger.warning(
                "start_session: could not extract conversation ID from agy log — "
                "session resumption will be unavailable for this job"
            )
        handle = GoogleSessionHandle(id=str(uuid.uuid4()), external_id=actual_session_id)
        reply = await self._parse_with_nudge(actual_session_id or "", data["response"])
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> GoogleSessionHandle:
        """Return a handle ready for send_message.

        With external_id: agy holds session history on disk; no subprocess call
        needed — just return a handle wrapping the conversation UUID.

        Without external_id: unrecoverable in the subprocess model. Raise loudly.
        """
        if external_id is not None:
            return GoogleSessionHandle(id=str(uuid.uuid4()), external_id=external_id)

        raise RuntimeError(
            "GoogleCliBackend.restore_session: external_id is None — cannot "
            "resume without a conversation UUID. This job's session_external_id was "
            "never persisted; mark the job failed and restart from pending."
        )

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Send a message to an existing session using --conversation mode.

        Spawns: agy --dangerously-skip-permissions --conversation <uuid> -p <text>
        """
        if not isinstance(handle, GoogleSessionHandle):
            raise TypeError(
                f"expected GoogleSessionHandle, got {type(handle).__name__}"
            )
        if handle.external_id is None:
            raise RuntimeError(
                "GoogleSessionHandle.external_id is None — cannot send message "
                "without a valid conversation UUID."
            )
        cmd = [
            "agy",
            "--dangerously-skip-permissions",
            "--conversation", handle.external_id,
            "-p", text,
        ]
        data = await self._run(cmd, handle.external_id)
        return await self._parse_with_nudge(handle.external_id, data["response"])

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op: the subprocess has already exited when start_session/send_message returned."""
        pass

    async def run_research(self, agent_name: str, query: str) -> str:
        """One-shot, non-interactive research via inline system prompt + agy's built-in search tools.

        Loads the Google-specific research prompt for agent_name, combines it with query,
        and runs agy in -p mode. Returns data["response"] raw — NO sentinel parsing.
        Uses RESEARCH_TIMEOUT. On any failure the caller must fall back to the NONE placeholder.
        """
        from pathlib import Path

        _PROMPT_MAP = {
            "cv-research": "GEMINI_CV_RESEARCH.md",
            "cl-research": "GEMINI_CL_RESEARCH.md",
        }
        if agent_name not in _PROMPT_MAP:
            raise ValueError(f"Unknown research agent: {agent_name!r}")

        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        system_prompt = (prompts_dir / _PROMPT_MAP[agent_name]).read_text(encoding="utf-8")

        cmd = [
            "agy",
            "--dangerously-skip-permissions",
            "-p", f"{system_prompt}\n\n{query}",
        ]
        data = await self._run(cmd, f"research:{agent_name}", self.RESEARCH_TIMEOUT)
        return data["response"]
