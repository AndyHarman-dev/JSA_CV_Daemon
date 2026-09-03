"""ClaudeCliBackend: subprocess -p implementation of AgentBackend for the Claude CLI.

Drives `claude` via non-interactive print mode (`-p`):
  - Fresh session:  claude --output-format text --system-prompt <sys> --session-id <uuid> -p <msg>
  - Subsequent msg: claude --output-format text --resume <uuid> -p <msg>

Session state is stored by the Claude CLI daemon/filesystem; JSA only needs to
remember the session UUID (stored in job.session_external_id).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from jsa.agents._subprocess import run_killable, run_killable_streaming
from jsa.agents.base import (
    AgentBackend,
    AgentChunk,
    AgentLimitReached,
    AgentReply,
    HistoryTurn,
    OnChunk,
    SessionHandle,
)
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
    # `claude --output-format stream-json --include-partial-messages --verbose`
    # emits real NDJSON content/thinking deltas (verified live, see the
    # agent-chat-upgrade plan's Phase 6 "Verified facts" section) — the richest
    # channel of any backend (content AND reasoning).
    supports_streaming = True

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

    @staticmethod
    def _to_streaming_cmd(cmd: list[str]) -> list[str]:
        """Swap ``--output-format text`` for the NDJSON streaming flags. ``cmd`` is
        always built with ``--output-format text`` first (see start_session/
        send_message below) so every streaming caller shares one conversion point."""
        out = list(cmd)
        idx = out.index("--output-format")
        out[idx + 1] = "stream-json"
        out.extend(["--include-partial-messages", "--verbose"])
        return out

    async def _run_streaming(
        self,
        cmd: list[str],
        context: str,
        on_chunk: OnChunk,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> str:
        """Streaming counterpart of ``_run``: same exit-code/session-expiry/quota
        parity surface, but driven off NDJSON events instead of plain text (see
        the module docstring's whitelist-parser rationale).

        The parser WHITELISTS: only ``type == "stream_event"`` ->
        ``event.type == "content_block_delta"`` -> ``delta.type in
        {"text_delta", "thinking_delta"}`` become chunks, and the terminal
        ``{"type": "assistant"}`` event's content blocks become ``raw`` — every
        other event (``system``/hook output, ``rate_limit_event``, ``result``) is
        either ignored for chunk purposes or consulted only for the two
        detection points below. A blacklist would leak hook output into the
        user's thread (live-captured: hook_started/hook_response system events
        carry arbitrary text in their ``output`` field).

        Quota/session detection under stream-json surfaces as JSON events
        (``rate_limit_event``, an error-shaped ``result``) rather than the
        raw-text ``_LIMIT_KEYWORDS`` scan ``_parse_with_nudge`` uses for
        ``--output-format text`` — that scan is unaffected; this is a parallel,
        additive detection path for the streaming call site only.
        """
        eff_timeout = timeout if timeout is not None else self._timeout
        streaming_cmd = self._to_streaming_cmd(cmd)

        assembled_parts: list[str] = []
        fallback_parts: list[str] = []
        limit_event: dict | None = None

        async def on_line(raw_line: bytes) -> None:
            nonlocal limit_event
            text_line = raw_line.decode("utf-8", errors="replace").strip()
            if not text_line:
                return
            try:
                event = json.loads(text_line)
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            etype = event.get("type")
            if etype == "stream_event":
                inner = event.get("event") or {}
                if inner.get("type") != "content_block_delta":
                    return
                delta = inner.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    piece = delta.get("text", "")
                    if piece:
                        fallback_parts.append(piece)
                        await on_chunk(AgentChunk(kind="content", text=piece))
                elif dtype == "thinking_delta":
                    piece = delta.get("thinking") or delta.get("text", "")
                    if piece:
                        await on_chunk(AgentChunk(kind="reasoning", text=piece))
            elif etype == "assistant":
                message = event.get("message") or {}
                blocks = message.get("content") or []
                text_parts = [
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if text_parts:
                    assembled_parts.append("".join(text_parts))
            elif etype == "rate_limit_event":
                limit_event = event
            elif etype == "result":
                if event.get("is_error") or event.get("subtype") not in (None, "success"):
                    limit_event = limit_event or event

        returncode, stdout_bytes, stderr_bytes = await run_killable_streaming(
            streaming_cmd,
            timeout=eff_timeout,
            cwd=str(cwd) if cwd is not None else None,
            label="claude CLI",
            on_line=on_line,
        )

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.debug("claude CLI (streaming) stderr: %s", stderr_text)

        # raw reconstruction: prefer the terminal assistant event's assembled
        # text (most robust — matches exactly what the CLI considers "the
        # reply"); fall back to the joined text_delta stream if no terminal
        # assistant event arrived (defensive — should not happen in practice).
        raw = "".join(assembled_parts) if assembled_parts else "".join(fallback_parts)

        if returncode != 0:
            if stderr_text:
                logger.warning("claude CLI stderr (exit %d): %s", returncode, stderr_text)
            ctx = f" [{context}]" if context else ""
            if "No conversation found" in stderr_text:
                raise ClaudeSessionExpiredError(
                    f"Claude session expired{ctx}: {stderr_text}. "
                    "Reset this job to restart from scratch."
                )
            detail = stderr_text or raw.strip() or "(no output)"
            raise ClaudeCliError(
                f"claude CLI failed (exit {returncode}){ctx}: {detail}"
            )

        if limit_event is not None:
            raise AgentLimitReached(json.dumps(limit_event)[:500])

        return raw

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
        on_chunk: OnChunk | None = None,
    ) -> tuple[ClaudeSessionHandle, AgentReply]:
        """Open a fresh claude CLI session and return the handle + first reply.

        Spawns: claude --output-format text --system-prompt <sys>
                        --session-id <uuid> -p <initial_user_msg>
        (or the --output-format stream-json variant when ``on_chunk`` is given —
        see _run_streaming.)
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
        raw = await self._run_dispatch(cmd, session_id, on_chunk)
        handle = ClaudeSessionHandle(id=str(uuid.uuid4()), external_id=session_id)
        reply = await self._parse_with_nudge(session_id, raw)
        return handle, reply

    async def _run_dispatch(
        self, cmd: list[str], context: str, on_chunk: OnChunk | None
    ) -> str:
        """Dispatch to the NDJSON streaming path when a callback is in hand, else
        the plain synchronous ``_run``. NOT wrapped in a fallback-and-retry: once
        the subprocess has actually run, re-issuing the command would send a
        second turn to a stateful ``--resume`` session, which is unsafe. The
        "streaming must never fail a job" contract is honored one layer down
        instead — every per-chunk failure inside ``on_line``/``on_chunk`` is
        swallowed by ``run_killable_streaming``/``ChunkAccumulator``, so a bug in
        THAT path can never surface here; a genuine subprocess-level failure
        (session expiry, quota, non-zero exit) is a real signal and propagates
        exactly as it would from the non-streaming ``_run``."""
        if on_chunk is None:
            return await self._run(cmd, context)
        return await self._run_streaming(cmd, context, on_chunk)

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

    async def send_message(
        self, handle: SessionHandle, text: str, on_chunk: OnChunk | None = None
    ) -> AgentReply:
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
        raw = await self._run_dispatch(cmd, handle.external_id, on_chunk)
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
