"""Regression tests for jsa.agents._subprocess.run_killable_streaming.

Uses a real (fake) executable that dribbles NDJSON lines with a delay, asserting
chunks arrive BEFORE the process exits and that a timeout still kills the whole
process group — same pattern as test_subprocess_killable.py's run_killable tests.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from jsa.agents._subprocess import run_killable_streaming
from jsa.agents.base import AgentTimeout

pytestmark = pytest.mark.asyncio

_DRIBBLE_SCRIPT = (
    "import sys, time\n"
    "for i in range(5):\n"
    "    print('{\"line\": %d}' % i, flush=True)\n"
    "    time.sleep(0.05)\n"
)

_SLEEP_FOREVER_SCRIPT = (
    "import sys, time\n"
    "print('{\"line\": 0}', flush=True)\n"
    "time.sleep(30)\n"
)


def _pgid_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


class TestChunksArriveBeforeExit:
    async def test_on_line_called_before_process_completes(self):
        received: list[bytes] = []
        completion_order: list[str] = []

        async def on_line(line: bytes) -> None:
            received.append(line)
            completion_order.append("chunk")

        result = await run_killable_streaming(
            ["python3", "-c", _DRIBBLE_SCRIPT],
            timeout=10,
            label="test dribble",
            on_line=on_line,
        )
        completion_order.append("done")
        assert result.returncode == 0
        assert len(received) == 5
        # All 5 chunks arrived before we recorded "done" (they were streamed
        # in, not just parsed from the final buffered result).
        assert completion_order == ["chunk"] * 5 + ["done"]
        assert result.stdout.count(b"\n") == 5


class TestTimeoutStillKillsGroup:
    async def test_timeout_kills_process_group(self, monkeypatch):
        captured: dict = {}
        real_create = asyncio.create_subprocess_exec

        async def _wrapped(*args, **kwargs):
            proc = await real_create(*args, **kwargs)
            captured["pid"] = proc.pid
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _wrapped)

        with pytest.raises(AgentTimeout):
            await run_killable_streaming(
                ["python3", "-c", _SLEEP_FOREVER_SCRIPT],
                timeout=0.3,
                label="test sleeper",
            )
        assert "pid" in captured
        await asyncio.sleep(0.1)
        assert not _pgid_is_alive(captured["pid"])


class TestOnLineExceptionSwallowed:
    async def test_broken_on_line_does_not_break_drain(self):
        async def broken_on_line(line: bytes) -> None:
            raise RuntimeError("boom")

        result = await run_killable_streaming(
            ["python3", "-c", _DRIBBLE_SCRIPT],
            timeout=10,
            label="test dribble",
            on_line=broken_on_line,
        )
        assert result.returncode == 0
        assert result.stdout.count(b"\n") == 5
