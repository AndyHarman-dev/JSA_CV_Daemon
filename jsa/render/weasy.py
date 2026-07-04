"""WeasyPrintRenderer: Markdown -> HTML -> PDF via WeasyPrint."""

import asyncio
from pathlib import Path

from markdown_it import MarkdownIt

from jsa.render.base import Renderer

_STYLES_PATH = Path(__file__).parent / "styles.css"

# WeasyPrint's underlying fontconfig/pango stack performs non-thread-safe global
# initialization (FcInitLoadOwnConfigAndFonts re-parses config when the font
# cache looks stale, e.g. after a long system sleep). Two concurrent write_pdf
# calls racing that init corrupts fontconfig's internal state and segfaults the
# whole process. renderer_for() returns a fresh WeasyPrintRenderer per call, so
# this lock must live at module scope to serialize across all instances/jobs.
_RENDER_LOCK = asyncio.Lock()

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
{css}
</style></head><body>
{body}
</body></html>"""


class WeasyPrintRenderer(Renderer):
    name = "weasyprint"

    async def render(self, markdown: str, output_path: Path) -> None:
        def _write_pdf() -> None:
            import weasyprint  # local import keeps the module load-time cost zero

            # Read CSS fresh each call — user edits take effect without restart.
            css = _STYLES_PATH.read_text(encoding="utf-8")

            # Convert Markdown → HTML with table and strikethrough extensions.
            # html=True lets WeasyPrint receive raw HTML from model output (e.g.
            # <div align="center">) rather than stripping or escaping it.
            md = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
            body_html = md.render(markdown)

            # Build HTML shell using str.replace to avoid .format() KeyError on CSS with {}.
            html_string = (
                _HTML_TEMPLATE
                .replace("{css}", css)
                .replace("{body}", body_html)
            )

            # Create output directory and write PDF.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            weasyprint.HTML(string=html_string).write_pdf(str(output_path))

        async with _RENDER_LOCK:
            await asyncio.to_thread(_write_pdf)
