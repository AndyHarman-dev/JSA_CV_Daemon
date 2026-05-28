"""Regression tests for BF-11/BF-13: HTML handling in the PDF renderer.

BF-11 established that raw HTML tags from model output must not appear as escaped
literal text (e.g. &lt;div&gt;) in the PDF.

BF-13 changes the strategy: instead of stripping HTML tags before rendering,
markdown-it is now configured with html=True so that raw HTML from the model
(e.g. <div align="center">) passes through to WeasyPrint intact. WeasyPrint then
renders the HTML natively, preserving centering, bold, and other styling.

Tests in this module verify the pass-through behaviour through the public render()
interface, inspecting the HTML string that WeasyPrintRenderer constructs and passes
to WeasyPrint.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jsa.render.weasy import WeasyPrintRenderer


# ---------------------------------------------------------------------------
# Helper fixture: captures the HTML string WeasyPrint sees, without calling
# the real WeasyPrint library.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_weasyprint():
    """Patch weasyprint.HTML and return the mock so tests can inspect calls."""
    with patch("weasyprint.HTML") as mock_html:
        yield mock_html


# ---------------------------------------------------------------------------
# BF-11 HTML-strip tests
# ---------------------------------------------------------------------------


class TestHtmlStrippedBeforeRender:
    async def test_div_tag_passed_through_text_preserved(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """<div align="center"> passes through to WeasyPrint; the name text is preserved.

        Pre-fix: markdown-it (html=False) would escape the tag as
        &lt;div align="center"&gt; — visible junk in the PDF.
        BF-13 fix: html=True lets the tag pass through intact; WeasyPrint renders it.
        """
        md = "<div align=\"center\">Jane Doe</div>"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # The text content must survive.
        assert "Jane Doe" in html_string
        # The tag passes through (html=True).
        assert "<div" in html_string
        # The escaped form must be absent (proves the tag was not escaped).
        assert "&lt;div" not in html_string

    async def test_multiple_inline_tags_passed_through(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Multiple raw HTML tags (e.g. <br>) pass through to WeasyPrint."""
        md = "# Hello\n<br>\nworld"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # With html=True, <br> passes through rather than being stripped.
        assert "br" in html_string
        # The escaped form must be absent (proves the tag was not escaped).
        assert "&lt;br" not in html_string
        # The heading text must be present.
        assert "Hello" in html_string

    async def test_heading_tag_passed_through(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """A raw <h1> tag in the markdown input passes through to WeasyPrint.

        With html=True, markdown-it treats <h1>...</h1> as an HTML block and
        passes it through intact. WeasyPrint renders it as a real h1 element.
        """
        md = "<h1>Raw heading tag</h1>"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # The text inside the tag must be present in the output.
        assert "Raw heading tag" in html_string
        # The tag passes through (html=True); WeasyPrint sees a real h1.
        assert "<h1>" in html_string

    async def test_clean_markdown_unaffected(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Markdown with no raw HTML tags is rendered normally.

        The strip must not disturb markdown-generated HTML tags (<h1>, <strong>).
        The regex only operates on the source markdown before md.render() is called,
        so generated HTML in the output is never touched.
        """
        md = "# Title\n\n**bold** and _italic_"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # Generated HTML from markdown-it must still be present.
        assert "<h1>" in html_string
        assert "<strong>" in html_string

    async def test_table_still_renders_after_strip(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """GFM table syntax is unaffected by the HTML strip.

        Table rows/cells contain no <...> in source Markdown, so the regex
        has nothing to remove. Generated <table> HTML must still appear.
        """
        md = (
            "| Name  | Score |\n"
            "| ----- | ----- |\n"
            "| Alice | 95    |\n"
        )
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]
        assert "<table>" in html_string

    async def test_mixed_html_and_markdown(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Markdown mixed with raw HTML: HTML passes through, markdown structure preserved."""
        md = (
            "<div align=\"center\">Jane Doe</div>\n\n"
            "## Experience\n\n"
            "- Did **great** work\n"
        )
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # Raw div tag passes through (html=True).
        assert "<div" in html_string
        assert "&lt;div" not in html_string
        # Content preserved.
        assert "Jane Doe" in html_string
        # Markdown-generated structure preserved.
        assert "<h2>" in html_string
        assert "<strong>" in html_string

    async def test_no_tags_input_is_noop(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Markdown with no HTML tags passes through the strip unchanged."""
        md = "No tags here"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        assert "No tags here" in html_string

    async def test_autolink_email_preserved(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """CommonMark autolink <jane.doe@example.com> is preserved by the narrowed regex.

        The BF-11 fix uses r'</?[a-zA-Z][a-zA-Z0-9-]*(\s[^>]*)?\s*/?>' which requires
        the tag name to start with a letter. An email address like jane.doe@example.com
        does not match that pattern (the dot after 'jane' exits the name character class),
        so the autolink is left untouched and markdown-it renders it as an anchor.
        """
        md = "<jane.doe@example.com>"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # The email address must survive in the rendered output.
        assert "jane.doe@example.com" in html_string
        # markdown-it renders CommonMark autolinks as <a href="mailto:..."> anchors.
        assert "mailto:" in html_string

    async def test_prose_comparison_preserved(
        self, tmp_path: Path, mock_weasyprint: MagicMock
    ) -> None:
        """Prose with bare comparison operators (<, >) is preserved by the narrowed regex.

        The old broad regex r'<[^>]+>' would silently delete '<2%' up to the next '>',
        swallowing part of the text. The narrowed regex requires a letter after '<', so
        numeric comparisons are never matched and both sides survive unchanged.
        """
        md = "Error rate <2% and throughput >3x"
        renderer = WeasyPrintRenderer()
        await renderer.render(md, tmp_path / "out.pdf")

        html_string = mock_weasyprint.call_args.kwargs["string"]

        # Both sides of the comparison operators must appear in the rendered output.
        assert "2%" in html_string
        assert "3x" in html_string
