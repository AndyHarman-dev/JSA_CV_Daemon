"""GeminiCliBackend: subprocess -p implementation of AgentBackend for the Gemini CLI.

Drives `gemini` via non-interactive print mode (`-p`) with JSON output (`-o json`):
  - Fresh session:  gemini --skip-trust --session-id <uuid> -p <sys+msg> -o json
  - Subsequent msg: gemini --skip-trust --resume <uuid> -p <msg> -o json

Session state is stored by the Gemini CLI filesystem; JSA only needs to remember
the session UUID (stored in job.session_external_id).

No pty is required: -p mode is fully non-interactive and the session persists on
disk between invocations. end_session is a no-op because the subprocess has
already exited.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass

from jsa.agents.base import AgentBackend, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError, parse_reply

logger = logging.getLogger(__name__)


class GeminiCliError(RuntimeError):
    """Raised when the gemini subprocess fails with no usable output."""


class GeminiSessionExpiredError(GeminiCliError):
    """Raised when gemini reports the session ID is no longer known."""


@dataclass(kw_only=True)
class GeminiSessionHandle(SessionHandle):
    """Session handle for GeminiCliBackend — carries the gemini CLI session UUID."""
    id: str
    external_id: str | None  # gemini session UUID, used for --resume


class GeminiCliBackend(AgentBackend):
    """AgentBackend that drives the `gemini` CLI via subprocess in -p (print) mode.

    Each call to start_session or send_message spawns a short-lived subprocess.
    No pty is needed: -p mode is fully non-interactive and streams to stdout as JSON.
    end_session is a no-op because the subprocess has already exited.
    """

    name = "gemini-cli"

    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Internal subprocess runner (runs in a thread via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _run(self, cmd: list[str], context: str = "") -> dict:
        """Run a gemini CLI command with -o json. Returns the parsed JSON dict.

        The caller extracts data["response"] for the reply text and
        data.get("session_id") if needed.

        Raises AgentTimeout if the process exceeds self._timeout seconds.
        Raises GeminiSessionExpiredError if the subprocess exits non-zero with
        empty stdout and stderr contains text suggesting the session is unknown.
        Raises GeminiCliError if the subprocess exits non-zero with empty stdout
        for any other reason, or if the JSON output cannot be parsed.
        stderr is logged at WARNING on nonzero exit, at DEBUG otherwise.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgentTimeout(
                f"gemini CLI timed out after {self._timeout}s"
            ) from exc

        ctx = f" [{context}]" if context else ""

        # Decode stderr once; log at the appropriate level based on exit code.
        stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            if stderr_text:
                logger.warning("gemini CLI stderr (exit %d): %s", result.returncode, stderr_text)
        else:
            if stderr_text:
                logger.debug("gemini CLI stderr: %s", stderr_text)

        stdout = result.stdout.decode("utf-8", errors="replace")

        if result.returncode != 0 and not stdout.strip():
            # Subprocess failed and produced no usable output — raise rather than
            # returning an empty dict that will cause a misleading KeyError.
            # NOTE: The exact stderr string for session-not-found is TBD pending
            # real-CLI observation. Conservative check: both "session" and "not found"
            # (case-insensitive) present in stderr.
            stderr_lower = stderr_text.lower()
            if "session" in stderr_lower and "not found" in stderr_lower:
                raise GeminiSessionExpiredError(
                    f"Gemini session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            raise GeminiCliError(
                f"gemini CLI failed (exit {result.returncode}){ctx}: {stderr_text or '(no stderr)'}"
            )

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise GeminiCliError(
                f"gemini CLI returned non-JSON output{ctx}: {stdout[:200]!r}"
            ) from exc

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
                "gemini",
                "--skip-trust",
                "--resume", session_id,
                "-p", nudge,
                "-o", "json",
            ]
            nudge_data = await asyncio.to_thread(self._run, nudge_cmd, session_id)
            return parse_reply(nudge_data["response"])  # Propagate on second failure

    # ------------------------------------------------------------------
    # AgentBackend interface
    # ------------------------------------------------------------------

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[GeminiSessionHandle, AgentReply]:
        """Open a fresh gemini CLI session and return the handle + first reply.

        Spawns: gemini --skip-trust --session-id <uuid>
                        -p <system_prompt + initial_user_msg> -o json
        """
        session_id = str(uuid.uuid4())
        cmd = [
            "gemini",
            "--skip-trust",
            "--session-id", session_id,
            "-p", f"{system_prompt}\n\n{initial_user_msg}",
            "-o", "json",
        ]
        data = await asyncio.to_thread(self._run, cmd, session_id)
        # Use the session_id from JSON output (may differ if CLI regenerated it)
        actual_session_id = data.get("session_id", session_id)
        handle = GeminiSessionHandle(id=str(uuid.uuid4()), external_id=actual_session_id)
        reply = await self._parse_with_nudge(actual_session_id, data["response"])
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> GeminiSessionHandle:
        """Return a handle ready for send_message.

        With external_id: the Gemini CLI holds the session history on disk; no
        subprocess call needed — just return a handle wrapping the UUID. Native
        --resume is now available in Gemini CLI v0.41.2+.

        Without external_id: this is an unrecoverable situation in the subprocess
        model. Raise RuntimeError so the orchestrator marks the job failed loudly.
        """
        if external_id is not None:
            # Gemini session state is persisted by the CLI; nothing to do here.
            return GeminiSessionHandle(id=str(uuid.uuid4()), external_id=external_id)

        # No external_id → no way to resume; fail loudly rather than silently
        # generating a spurious extra model turn.
        raise RuntimeError(
            "GeminiCliBackend.restore_session: external_id is None — cannot "
            "resume without a session UUID. This job's session_external_id was "
            "never persisted; mark the job failed and restart from pending."
        )

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Send a message to an existing session using --resume mode.

        Spawns: gemini --skip-trust --resume <session_id> -p <text> -o json
        """
        if not isinstance(handle, GeminiSessionHandle):
            raise TypeError(
                f"expected GeminiSessionHandle, got {type(handle).__name__}"
            )
        if handle.external_id is None:
            raise RuntimeError(
                "GeminiSessionHandle.external_id is None — cannot send message "
                "without a valid session UUID."
            )
        cmd = [
            "gemini",
            "--skip-trust",
            "--resume", handle.external_id,
            "-p", text,
            "-o", "json",
        ]
        data = await asyncio.to_thread(self._run, cmd, handle.external_id)
        return await self._parse_with_nudge(handle.external_id, data["response"])

    async def end_session(self, handle: SessionHandle) -> None:
        """No-op: the subprocess has already exited when start_session/send_message returned."""
        pass
