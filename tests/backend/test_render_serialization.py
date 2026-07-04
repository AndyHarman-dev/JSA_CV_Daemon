"""Regression test for the WeasyPrint fontconfig segfault (see CLAUDE.md /
plan history): two concurrent write_pdf calls raced fontconfig's non-thread-safe
global init and crashed the process. jsa.render.weasy._RENDER_LOCK must ensure
at most one write_pdf runs at a time, process-wide, even across independent
WeasyPrintRenderer instances (renderer_for() returns a fresh one per call).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from jsa.render.weasy import WeasyPrintRenderer


class _ConcurrencyProbe:
    """Tracks the max number of simultaneous entrants into a critical section."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.max_seen = 0

    def enter(self) -> None:
        with self._lock:
            self.current += 1
            self.max_seen = max(self.max_seen, self.current)

    def exit(self) -> None:
        with self._lock:
            self.current -= 1


async def test_concurrent_renders_never_overlap_write_pdf(tmp_path: Path) -> None:
    """Two renderer.render() calls launched together must serialize write_pdf."""
    probe = _ConcurrencyProbe()

    def fake_write_pdf(path: str) -> None:
        probe.enter()
        try:
            # Real blocking sleep (this runs in a real OS thread via
            # asyncio.to_thread) widens the window for a race to manifest.
            time.sleep(0.05)
            Path(path).write_bytes(b"PDF")
        finally:
            probe.exit()

    with patch("weasyprint.HTML") as mock_html:
        mock_html.return_value.write_pdf.side_effect = fake_write_pdf

        import asyncio

        # renderer_for() would hand back a fresh instance per call in
        # production; using two distinct instances here proves the lock is
        # module-level, not per-instance.
        r1 = WeasyPrintRenderer()
        r2 = WeasyPrintRenderer()
        await asyncio.gather(
            r1.render("# Doc A", tmp_path / "a.pdf"),
            r2.render("# Doc B", tmp_path / "b.pdf"),
        )

    assert probe.max_seen == 1, (
        "write_pdf ran concurrently across renderer instances; the fontconfig "
        "init race this test guards against can segfault the process"
    )
    assert (tmp_path / "a.pdf").read_bytes() == b"PDF"
    assert (tmp_path / "b.pdf").read_bytes() == b"PDF"
