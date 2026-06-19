"""OpenCodeCliBackend: subprocess -p implementation of AgentBackend for the `opencode` CLI.

Drives `opencode` via non-interactive print mode (`-p`) with plain-text output:
  - Fresh session:  opencode --session <uuid> -p "<sys>\n\n<user>" -m <model>
  - Resume session: opencode --session <external_id> -p "<msg>" -m <model>

Session state is persisted by the opencode CLI built-in. JSA only needs to
remember the session UUID (stored in job.session_external_id).

No pty is required: -p mode is fully non-interactive and the session persists on
disk between invocations. end_session is a no-op because the subprocess has
already exited.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError, parse_reply

logger = logging.getLogger(__name__)


class OpenCodeCliError(RuntimeError):
    """Raised when the opencode subprocess fails with no usable output."""


class OpenCodeSessionExpiredError(OpenCodeCliError):
    """Raised when opencode reports the session ID is no longer known."""


@dataclass(kw_only=True)
class OpenCodeSessionHandle(SessionHandle):
    """Session handle for OpenCodeCliBackend — carries the opencode session UUID."""
    id: str
    external_id: str | None  # opencode --session token


class OpenCodeCliBackend(AgentBackend):
    """AgentBackend that drives the `opencode` CLI via subprocess in -p (print) mode.

    Each call to start_session or send_message spawns a short-lived subprocess.
    No pty is needed: -p mode is fully non-interactive and streams to stdout.
    end_session is a no-op because the subprocess has already exited.
    """

    name = "opencode-cli"
    RESEARCH_TIMEOUT = 300.0  # web search + multiple fetches can exceed the 120s message-turn default

    def __init__(self, model: str = "opencode/nemotron-3-ultra-free", timeout: float = 120.0) -> None:
        self._model = model
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal subprocess runner (runs in a thread via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], context: str = "", timeout: float | None = None) -> str:
        """Run an opencode CLI command and return its stdout.

        Raises AgentTimeout if the process exceeds the effective timeout.
        Raises OpenCodeSessionExpiredError if the subprocess exits non-zero with
        empty stdout and stderr suggests the session is unknown.
        Raises OpenCodeCliError if the subprocess exits non-zero with empty stdout
        for any other reason.
        stderr is logged at WARNING on nonzero exit, at DEBUG otherwise.
        """
        eff_timeout = timeout if timeout is not None else self._timeout
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=eff_timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeout(
                f"opencode CLI timed out after {eff_timeout}s"
            ) from exc

        ctx = f" [{context}]" if context else ""

        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            if stderr_text:
                logger.warning("opencode CLI stderr (exit %d): %s", result.returncode, stderr_text)
        else:
            if stderr_text:
                logger.debug("opencode CLI stderr: %s", stderr_text)

        stdout = result.stdout.decode("utf-8", errors="replace").strip()

        if result.returncode != 0 and not stdout:
            stderr_lower = stderr_text.lower()
            if "session" in stderr_lower and ("not found" in stderr_lower or "unknown" in stderr_lower or "expired" in stderr_lower):
                raise OpenCodeSessionExpiredError(
                    f"OpenCode session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            raise OpenCodeCliError(
                f"opencode CLI failed (exit {result.returncode}){ctx}: {stderr_text or '(no stderr)'}"
            )

        return stdout

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _parse_with_nudge(self, session_id: str, raw: str) -> AgentReply:
        """Try parse_reply(raw); on 'no sentinel block' ProtocolError, nudge once.

        If the first parse succeeds, return the result immediately.
        If the reply is missing the sentinel block, log a warning, send a nudge
        via --session <session_id>, and return parse_reply of the nudge reply
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
                "opencode",
                "--session", session_id,
                "-p", nudge,
                "-m", self._model,
            ]
            raw2 = await asyncio.to_thread(self._run, nudge_cmd, session_id)
            return parse_reply(raw2)  # Propagate on second failure

    # ------------------------------------------------------------------
    # AgentBackend interface
    # ------------------------------------------------------------------

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[OpenCodeSessionHandle, AgentReply]:
        """Open a fresh opencode CLI session and return the handle + first reply.

        Spawns: opencode --session <uuid> -p "<system_prompt>\n\n<initial_user_msg>" -m <model>
        """
        session_id = str(uuid.uuid4())
        cmd = [
            "opencode",
            "--session", session_id,
            "-p", f"{system_prompt}\n\n{initial_user_msg}",
            "-m", self._model,
        ]
        raw = await asyncio.to_thread(self._run, cmd, session_id)
        handle = OpenCodeSessionHandle(id=str(uuid.uuid4()), external_id=session_id)
        reply = await self._parse_with_nudge(session_id, raw)
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> OpenCodeSessionHandle:
        """Return a handle ready for send_message.

        With external_id: opencode holds session history on disk; no subprocess
        call needed — just return a handle wrapping the session UUID.

        Without external_id: unrecoverable in the subprocess model. Raise loudly.
        """
        if external_id is not None:
            return OpenCodeSessionHandle(id=str(uuid.uuid4()), external_id=external_id)

        raise RuntimeError(
            "OpenCodeCliBackend.restore_session: external_id is None — cannot "
            "resume without a session UUID. This job's session_external_id was "
            "never persisted; mark the job failed and restart from pending."
        )

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Send a message to an existing session using --session mode.

        Spawns: opencode --session <external_id> -p <text> -m <model>
        """
        if not isinstance(handle, OpenCodeSessionHandle):
            raise TypeError(
                f"expected OpenCodeSessionHandle, got {type(handle).__name__}"
            )
        if handle.external_id is None:
            raise RuntimeError(
                "OpenCodeSessionHandle.external_id is None — cannot send message "
                "without a valid session UUID."
            )
        cmd = [
            "opencode",
            "--session", handle.external_id,
            "-p", text,
            "-m", self._model,
        ]
        raw = await asyncio.to_thread(self._run, cmd, handle.external_id)
        return await self._parse_with_nudge(handle.external_id, raw)

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op: the subprocess has already exited when start_session/send_message returned."""
        pass

    async def run_research(self, agent_name: str, query: str) -> str:
        """One-shot, non-interactive research via inline system prompt + opencode's built-in capabilities.

        Loads the OpenCode-specific research prompt for agent_name, combines it with query,
        and runs opencode in -p mode. Returns data["response"] raw — NO sentinel parsing.
        Uses RESEARCH_TIMEOUT. On any failure the caller must fall back to the NONE placeholder.
        """
        _PROMPT_MAP = {
            "cv-research": "OPENCODE_CV_RESEARCH.md",
            "cl-research": "OPENCODE_CL_RESEARCH.md",
        }
        if agent_name not in _PROMPT_MAP:
            raise ValueError(f"Unknown research agent: {agent_name!r}")

        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        system_prompt = (prompts_dir / _PROMPT_MAP[agent_name]).read_text(encoding="utf-8")

        cmd = [
            "opencode",
            "-p", f"{system_prompt}\n\n{query}",
            "-m", self._model,
        ]
        return await asyncio.to_thread(
            self._run, cmd, f"research:{agent_name}", self.RESEARCH_TIMEOUT
        )
