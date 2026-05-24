"""GeminiCliBackend: pty subprocess implementation of AgentBackend for the Gemini CLI."""

from __future__ import annotations

import asyncio
import logging
import re
import signal
import uuid
from dataclasses import dataclass
from typing import Any

from jsa.agents.base import AgentBackend, AgentReply, AgentTimeout, HistoryTurn, SessionHandle
from jsa.agents.protocol import parse_reply

logger = logging.getLogger(__name__)

# UUID pattern used to detect a gemini session id in its output (if ever supported)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass(kw_only=True)
class GeminiSessionHandle(SessionHandle):
    """Session handle for GeminiCliBackend; carries the pty process reference."""
    id: str
    external_id: str | None  # gemini session_id, if supported and detected
    pty: Any                 # ptyprocess.PtyProcess instance


async def _read_until_sentinel(pty: Any, timeout: float) -> str:
    """Read from pty file descriptor until <<<END>>> appears or timeout expires.

    Returns accumulated text decoded from pty bytes.
    Raises AgentTimeout if the deadline is reached without a sentinel.
    """
    buf = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AgentTimeout("timed out waiting for sentinel")
        try:
            chunk = await asyncio.wait_for(
                loop.run_in_executor(None, pty.read, 4096),
                timeout=remaining,
            )
            buf += chunk.decode("utf-8", errors="replace")
        except (EOFError, OSError):
            # pty closed or process exited
            break
        if "<<<END>>>" in buf:
            break
    return buf


def _extract_session_id(text: str) -> str | None:
    """Attempt to extract a UUID-like session ID from gemini CLI output."""
    match = _UUID_RE.search(text)
    return match.group(0) if match else None


class GeminiCliBackend(AgentBackend):
    """AgentBackend implementation that drives the `gemini` CLI via a pty subprocess."""

    name = "gemini-cli"

    def __init__(self, timeout: float = 120.0) -> None:
        self._timeout = timeout

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[GeminiSessionHandle, AgentReply]:
        """Open a fresh gemini CLI session and return the handle + first reply."""
        import ptyprocess  # type: ignore[import]

        pty = await asyncio.to_thread(
            ptyprocess.PtyProcess.spawn,
            ["gemini"],
        )

        # Combine system prompt and initial user message as the first input
        combined_input = f"{system_prompt}\n\n{initial_user_msg}\n"
        await asyncio.to_thread(pty.write, combined_input.encode("utf-8"))

        raw = await _read_until_sentinel(pty, self._timeout)

        external_id = _extract_session_id(raw)
        if external_id is None:
            logger.debug("GeminiCliBackend: no session ID detected in initial output")

        handle = GeminiSessionHandle(
            id=str(uuid.uuid4()),
            external_id=external_id,
            pty=pty,
        )
        reply = parse_reply(raw)
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> GeminiSessionHandle:
        """Reconstruct a previously-ended session without generating new assistant turns.

        Gemini CLI does not support native session resume as of v1.
        Fallback: send system_prompt + full history as a single combined message.
        This relies on prompt-determinism (the assistant asks the same follow-up
        given the same inputs) — see ARCH.md § Agent-session lifecycle.

        The returned handle is ready for send_message; no AgentReply is returned.
        """
        import ptyprocess  # type: ignore[import]

        logger.warning(
            "GeminiCliBackend: native resume not available; using history-replay fallback"
        )

        pty = await asyncio.to_thread(
            ptyprocess.PtyProcess.spawn,
            ["gemini"],
        )

        # Build a combined message: system prompt + alternating history turns
        parts = [system_prompt]
        for turn in history:
            parts.append(f"[{turn.role.upper()}]: {turn.content}")
        combined_input = "\n\n".join(parts) + "\n"

        await asyncio.to_thread(pty.write, combined_input.encode("utf-8"))

        # Drain the replay reply and discard it — we do not return it.
        raw = await _read_until_sentinel(pty, self._timeout)

        # Attempt to pick up a session id from the replay output (future-proofing)
        detected_id = _extract_session_id(raw)

        handle = GeminiSessionHandle(
            id=str(uuid.uuid4()),
            external_id=detected_id,
            pty=pty,
        )
        return handle

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        """Send a message to the running gemini pty and return the parsed reply."""
        assert isinstance(handle, GeminiSessionHandle)
        await asyncio.to_thread(handle.pty.write, (text + "\n").encode("utf-8"))
        raw = await _read_until_sentinel(handle.pty, self._timeout)
        return parse_reply(raw)

    async def end_session(self, handle: SessionHandle) -> None:
        """Terminate the gemini pty subprocess and reap the child process."""
        assert isinstance(handle, GeminiSessionHandle)
        try:
            handle.pty.kill(signal.SIGTERM)
        except Exception:
            logger.debug("GeminiCliBackend.end_session: SIGTERM failed (process may be gone)")
        try:
            handle.pty.close()
        except Exception:
            logger.debug("GeminiCliBackend.end_session: pty.close() failed")
