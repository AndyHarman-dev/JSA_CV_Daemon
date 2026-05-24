"""Shared pty helpers used by ClaudeCliBackend and GeminiCliBackend."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from jsa.agents.base import AgentTimeout

# UUID pattern used to detect a CLI session id in subprocess output
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _extract_session_id(text: str) -> str | None:
    """Attempt to extract a UUID-like session ID from CLI output."""
    match = _UUID_RE.search(text)
    return match.group(0) if match else None


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
        except asyncio.TimeoutError:
            raise AgentTimeout("timed out waiting for sentinel")
        except (EOFError, OSError):
            # pty closed or process exited
            break
        if "<<<END>>>" in buf:
            break
    return buf
