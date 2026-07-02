"""Regression tests for jsa.agents._subprocess.run_killable.

Bug: both CLI backends ran their subprocess via blocking subprocess.run()
inside asyncio.to_thread(...). Orchestrator.cancel_task()'s task.cancel()
cannot interrupt a blocking thread, so the underlying claude/agy process ran
to completion regardless — still burning API quota on "cancel". Fix:
run_killable spawns the command in its own process group and kills the whole
group on timeout or external cancellation. These tests verify a real child
process is actually killed (its process group ceases to exist), not just
that the coroutine returns/raises.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from jsa.agents._subprocess import run_killable
from jsa.agents.base import AgentTimeout

# A genuinely long-running child so we can prove it gets killed rather than
# completing before our assertions run.
_SLEEP_CMD = ["python3", "-c", "import time; time.sleep(30)"]


def _pgid_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)  # signal 0: existence check only, no actual kill
        return True
    except ProcessLookupError:
        return False


@pytest.fixture
def captured_pid(monkeypatch):
    """Wrap asyncio.create_subprocess_exec to record the spawned pid while
    still delegating to the real implementation, so tests can assert on
    actual OS process-group state without parsing `ps` output."""
    captured: dict = {}
    real_create = asyncio.create_subprocess_exec

    async def _wrapped(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        captured["pid"] = proc.pid  # pid == pgid: spawned with start_new_session=True
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _wrapped)
    return captured


class TestRunKillableCancellation:
    async def test_cancel_kills_the_process_group(self, captured_pid):
        """task.cancel() on the coroutine awaiting run_killable must kill the
        child's whole process group promptly, not leave it running."""
        task = asyncio.create_task(run_killable(_SLEEP_CMD, timeout=30, label="test sleeper"))
        # Let the subprocess actually start before cancelling.
        while "pid" not in captured_pid:
            await asyncio.sleep(0.02)
        pgid = captured_pid["pid"]
        assert _pgid_is_alive(pgid), "sanity check: process should be alive before cancel"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not _pgid_is_alive(pgid), (
            "process group must be dead immediately after cancellation lands — "
            "an alive group here means token burn continues after 'cancel'"
        )

    async def test_timeout_kills_the_process_group(self, captured_pid):
        """The AgentTimeout path must also kill the group, not just raise."""
        with pytest.raises(AgentTimeout):
            await run_killable(_SLEEP_CMD, timeout=0.3, label="test sleeper")

        assert "pid" in captured_pid
        assert not _pgid_is_alive(captured_pid["pid"]), (
            "process group must be dead after a timeout"
        )

    async def test_successful_run_returns_output(self):
        rc, out, err = await run_killable(
            ["python3", "-c", "print('hello')"], timeout=10, label="test echo"
        )
        assert rc == 0
        assert out.strip() == b"hello"
        assert err == b""

    async def test_nonzero_exit_returns_returncode_and_stderr(self):
        rc, out, err = await run_killable(
            ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"],
            timeout=10,
            label="test failure",
        )
        assert rc == 2
        assert out == b""
        assert err.strip() == b"boom"
