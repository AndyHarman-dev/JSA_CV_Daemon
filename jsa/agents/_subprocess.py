"""Shared async subprocess seam for CLI backends (claude_cli.py, google_cli.py).

Both `claude` and `agy` are launcher shims that fork a versioned child process
which does the real work (verified via `ps`: `.../bin/claude -> .../versions/X ->
.../versions/X`). Killing only the directly-spawned process would orphan that
child and leave it running — still burning API quota. `run_killable` therefore
spawns the command in its own process group (`start_new_session=True`) and, on
timeout or external cancellation, kills the *whole group* via `os.killpg`.

This is the one place killability is implemented and tested; both backends'
`_run` call it and keep their own result-parsing local.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from jsa.agents.base import AgentTimeout


class KillableResult(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


async def run_killable(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    label: str = "subprocess",
) -> KillableResult:
    """Spawn `cmd` as an async subprocess and return (returncode, stdout, stderr).

    The child is started in its own process group so the entire group can be
    killed, not just the direct child. On timeout, raises AgentTimeout after
    killing the group. On external cancellation (asyncio.CancelledError, e.g.
    from Orchestrator.cancel_task), the group is killed in `finally` and the
    CancelledError continues to propagate — no orphaned process survives to
    keep consuming quota.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,  # own process group -> os.killpg works
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        raise AgentTimeout(f"{label} timed out after {timeout}s") from None
    finally:
        if proc.returncode is None:  # still alive: timeout path or CancelledError
            try:
                # proc.pid IS the pgid here: start_new_session=True makes this
                # process its own session/group leader, so no os.getpgid lookup
                # is needed (and none of the TOCTOU risk a lookup would add).
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already exited between the check and the signal
            await proc.wait()
    return KillableResult(proc.returncode, out, err)


async def run_killable_streaming(
    cmd: list[str],
    *,
    timeout: float,
    cwd: str | None = None,
    label: str = "subprocess",
    on_line: Callable[[bytes], Awaitable[None]] | None = None,
) -> KillableResult:
    """Streaming counterpart of ``run_killable`` — reads ``stdout`` line-by-line
    (invoking ``on_line`` for each raw line, as it arrives) while draining
    ``stderr`` concurrently, so the OS pipe buffer never fills and deadlocks the
    child. Reproduces the IDENTICAL kill contract as ``run_killable``: same
    process-group spawn, same ``AgentTimeout`` path, same ``os.killpg`` cleanup
    on timeout or external cancellation (``asyncio.CancelledError`` continues to
    propagate after the group is killed in ``finally``).

    ``run_killable`` itself is intentionally left untouched — this is a NEW,
    parallel function (see jsa/agents/claude_cli.py's streaming path and the
    agent-chat-upgrade plan's Phase 7 item 7) so google_cli.py and
    run_research's use of the tested, unmodified ``run_killable`` keep their
    existing behavior byte-for-byte.

    ``on_line`` failures are swallowed here (best-effort, mirroring every other
    streaming call site's contract) so a broken callback can never prevent the
    subprocess's stdout from being fully drained and returned.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,  # own process group -> os.killpg works
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _drain_stdout() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            stdout_chunks.append(line)
            if on_line is not None:
                try:
                    await on_line(line)
                except Exception:  # noqa: BLE001 - streaming must never break the drain
                    pass

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    try:
        await asyncio.wait_for(
            asyncio.gather(_drain_stdout(), _drain_stderr(), proc.wait()),
            timeout,
        )
    except asyncio.TimeoutError:
        raise AgentTimeout(f"{label} timed out after {timeout}s") from None
    finally:
        if proc.returncode is None:  # still alive: timeout path or CancelledError
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # already exited between the check and the signal
            await proc.wait()
    return KillableResult(proc.returncode, b"".join(stdout_chunks), b"".join(stderr_chunks))
