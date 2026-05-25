"""Unit tests for Phase 7: Renderer (jsa.render.*)

Covers:
  - FakeRenderer (tests/backend/fakes/fake_renderer.py)
  - renderer_for() in jsa.render.registry
  - WeasyPrintRenderer in jsa.render.weasy

WeasyPrint has heavy system dependencies (cairo, pango). All tests that
exercise WeasyPrintRenderer patch `weasyprint.HTML` so the PDF write
never actually calls the system library.

asyncio_mode = "auto" is configured in pyproject.toml — async test
functions need no extra marks.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jsa.render.base import Renderer
from jsa.render.registry import renderer_for
from jsa.render.weasy import WeasyPrintRenderer
from tests.backend.fakes.fake_renderer import FakeRenderer, RenderCall


# ---------------------------------------------------------------------------
# FakeRenderer tests
# ---------------------------------------------------------------------------


class TestFakeRenderer:
    async def test_render_writes_stub_bytes(self, tmp_path: Path) -> None:
        """render() writes b"PDF" to the given output path."""
        out = tmp_path / "output.pdf"
        renderer = FakeRenderer()
        await renderer.render("# Hello", out)
        assert out.read_bytes() == b"PDF"

    async def test_render_creates_parent_directories(self, tmp_path: Path) -> None:
        """render() creates all missing parent directories."""
        out = tmp_path / "deep" / "nested" / "dir" / "output.pdf"
        renderer = FakeRenderer()
        await renderer.render("# Hello", out)
        assert out.parent.is_dir()
        assert out.exists()

    async def test_render_records_call(self, tmp_path: Path) -> None:
        """Each call to render() appends a RenderCall to self.calls."""
        out = tmp_path / "output.pdf"
        renderer = FakeRenderer()
        await renderer.render("# Hello", out)
        assert len(renderer.calls) == 1
        assert renderer.calls[0].markdown == "# Hello"
        assert renderer.calls[0].output_path == out

    async def test_render_records_multiple_calls(self, tmp_path: Path) -> None:
        """Multiple invocations produce multiple RenderCall entries in order."""
        renderer = FakeRenderer()
        out1 = tmp_path / "a.pdf"
        out2 = tmp_path / "b.pdf"
        await renderer.render("# Doc A", out1)
        await renderer.render("# Doc B", out2)
        assert len(renderer.calls) == 2
        assert renderer.calls[0].markdown == "# Doc A"
        assert renderer.calls[1].markdown == "# Doc B"

    async def test_render_calls_start_empty(self) -> None:
        """A freshly constructed FakeRenderer has no recorded calls."""
        renderer = FakeRenderer()
        assert renderer.calls == []

    def test_fake_renderer_is_renderer_subclass(self) -> None:
        """FakeRenderer must subclass Renderer (isinstance check)."""
        assert isinstance(FakeRenderer(), Renderer)


# ---------------------------------------------------------------------------
# renderer_for() registry tests
# ---------------------------------------------------------------------------


class TestRendererFor:
    def test_weasyprint_returns_weasyprint_renderer(self) -> None:
        """renderer_for("weasyprint") returns a WeasyPrintRenderer instance."""
        r = renderer_for("weasyprint")
        assert isinstance(r, WeasyPrintRenderer)

    def test_weasyprint_is_renderer_instance(self) -> None:
        """WeasyPrintRenderer is a Renderer (isinstance check on ABC)."""
        r = renderer_for("weasyprint")
        assert isinstance(r, Renderer)

    def test_unknown_name_raises_key_error(self) -> None:
        """renderer_for() raises KeyError for an unrecognised name."""
        with pytest.raises(KeyError):
            renderer_for("nonexistent_renderer")

    def test_unknown_name_error_message_contains_name(self) -> None:
        """The KeyError message includes the unknown name for debuggability."""
        with pytest.raises(KeyError, match="nonexistent_renderer"):
            renderer_for("nonexistent_renderer")

    def test_each_call_returns_new_instance(self) -> None:
        """renderer_for() creates a fresh instance each call (no shared state)."""
        r1 = renderer_for("weasyprint")
        r2 = renderer_for("weasyprint")
        assert r1 is not r2


# ---------------------------------------------------------------------------
# WeasyPrintRenderer tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_weasyprint():
    """Patch weasyprint.HTML for the duration of a test.

    Returns the mock object so tests can inspect call arguments and
    optionally attach side_effects.
    """
    with patch("weasyprint.HTML") as mock_html:
        yield mock_html


class TestWeasyPrintRenderer:
    async def test_render_creates_output_directory(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """render() creates parent directories that do not yet exist."""
        out = tmp_path / "new_dir" / "sub" / "output.pdf"
        renderer = WeasyPrintRenderer()
        await renderer.render("# Hello", out)
        assert out.parent.is_dir()

    async def test_render_produces_file_at_output_path(
        self, tmp_path: Path
    ) -> None:
        """render() writes a file at output_path when PDF write is exercised.

        The mock's write_pdf side_effect actually creates the file so we
        can verify the path is used correctly.
        """
        out = tmp_path / "output.pdf"
        with patch("weasyprint.HTML") as mock_html:
            # Side-effect: create the file at the path passed to write_pdf.
            mock_html.return_value.write_pdf.side_effect = (
                lambda p: Path(p).write_bytes(b"PDF")
            )
            renderer = WeasyPrintRenderer()
            await renderer.render("# Hello", out)
        assert out.exists()
        assert out.read_bytes() == b"PDF"

    async def test_render_table_markdown_no_error(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Markdown containing a GFM table renders without raising an exception."""
        md = (
            "| Name | Score |\n"
            "| --- | --- |\n"
            "| Alice | 95 |\n"
            "| Bob | 87 |\n"
        )
        renderer = WeasyPrintRenderer()
        # Should not raise.
        await renderer.render(md, tmp_path / "table.pdf")

    async def test_render_strikethrough_markdown_no_error(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Markdown containing ~~strikethrough~~ renders without raising."""
        md = "This is ~~old text~~ and this is new."
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "strike.pdf")

    async def test_css_is_embedded_in_html_passed_to_weasyprint(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """The CSS from styles.css is embedded in the HTML string passed to weasyprint.HTML."""
        renderer = WeasyPrintRenderer()
        await renderer.render("# Hello", tmp_path / "out.pdf")

        # weasyprint.HTML was called with string=<html> kwarg.
        call_kwargs = mock_weasyprint.call_args.kwargs
        assert "string" in call_kwargs, (
            "weasyprint.HTML should be called with string=<html> keyword argument"
        )
        html_string = call_kwargs["string"]
        # A known selector from styles.css — confirms the CSS file content was embedded.
        assert "@page" in html_string
        assert "letter" in html_string

    async def test_weasyprint_html_called_with_string_kwarg(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """weasyprint.HTML is called exactly once with a non-empty string= kwarg."""
        renderer = WeasyPrintRenderer()
        await renderer.render("# Test", tmp_path / "out.pdf")

        mock_weasyprint.assert_called_once()
        call_kwargs = mock_weasyprint.call_args.kwargs
        html_str = call_kwargs.get("string", "")
        assert len(html_str) > 0

    async def test_write_pdf_called_with_output_path_string(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """write_pdf() is called with str(output_path)."""
        out = tmp_path / "cv.pdf"
        renderer = WeasyPrintRenderer()
        await renderer.render("# CV", out)

        mock_html_instance = mock_weasyprint.return_value
        mock_html_instance.write_pdf.assert_called_once_with(str(out))

    async def test_css_read_fresh_each_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CSS is read fresh from disk on every render() call (no caching).

        We swap _STYLES_PATH to a temporary file, render once, update the
        file, render again, and assert that the two captured HTML payloads
        differ — proving the file is re-read each time.
        """
        css_file = tmp_path / "custom.css"
        css_file.write_text("body { color: red; }", encoding="utf-8")

        import jsa.render.weasy as weasy_module

        monkeypatch.setattr(weasy_module, "_STYLES_PATH", css_file)

        captured: list[str] = []

        def capturing_html(*, string: str) -> MagicMock:
            captured.append(string)
            mock = MagicMock()
            return mock

        with patch("weasyprint.HTML", side_effect=capturing_html):
            renderer = WeasyPrintRenderer()
            await renderer.render("# First", tmp_path / "first.pdf")

            # Update the CSS file before the second render.
            css_file.write_text("body { color: blue; }", encoding="utf-8")

            await renderer.render("# Second", tmp_path / "second.pdf")

        assert len(captured) == 2
        assert "color: red" in captured[0], "First render should embed red CSS"
        assert "color: blue" in captured[1], "Second render should embed updated blue CSS"

    async def test_markdown_body_appears_in_html(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """The rendered Markdown body appears in the HTML string passed to weasyprint."""
        md = "# My Great Resume\n\nI am a **great** candidate."
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]
        # markdown-it-py converts # to <h1> and ** to <strong>
        assert "<h1>" in html_string
        assert "<strong>" in html_string

    async def test_table_html_rendered_into_string(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """A Markdown table is converted to <table> tags in the HTML."""
        md = (
            "| Col1 | Col2 |\n"
            "| ---- | ---- |\n"
            "| A    | B    |\n"
        )
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]
        assert "<table>" in html_string

    async def test_strikethrough_html_rendered_into_string(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """~~strikethrough~~ is converted to strikethrough HTML tags.

        markdown-it-py renders strikethrough as <s> (not <del>), which is
        the standard GFM strikethrough tag.
        """
        md = "This is ~~deleted~~ text."
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]
        # markdown-it-py uses <s> for GFM strikethrough
        assert "<s>" in html_string


# ---------------------------------------------------------------------------
# Renderer ABC instantiation guard
# ---------------------------------------------------------------------------


def test_renderer_abc_cannot_be_instantiated():
    with pytest.raises(TypeError):
        Renderer()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# WeasyPrintRenderer.name attribute
# ---------------------------------------------------------------------------


def test_weasyprint_renderer_name():
    r = renderer_for("weasyprint")
    assert r.name == "weasyprint"
