"""FakeRenderer — deterministic Renderer implementation for testing.

Writes stub bytes (b"PDF") to output_path, creates parent directories,
and records every call so tests can assert on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jsa.render.base import Renderer


@dataclass
class RenderCall:
    """Records arguments from a single render() invocation."""
    markdown: str
    output_path: Path


class FakeRenderer(Renderer):
    """Test stub for Renderer.

    Writes b"PDF" to output_path (creating parent directories) and
    appends a RenderCall to self.calls for each invocation.

    Usage::

        renderer = FakeRenderer()
        await renderer.render("# Hello", Path("/tmp/out/doc.pdf"))
        assert len(renderer.calls) == 1
        assert renderer.calls[0].markdown == "# Hello"
        assert Path("/tmp/out/doc.pdf").read_bytes() == b"PDF"
    """

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[RenderCall] = []

    async def render(self, markdown: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"PDF")
        self.calls.append(RenderCall(markdown=markdown, output_path=output_path))
