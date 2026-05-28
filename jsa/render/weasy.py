"""WeasyPrintRenderer: Markdown -> HTML -> PDF via WeasyPrint."""

import asyncio
import re
from pathlib import Path

from markdown_it import MarkdownIt

from jsa.render.base import Renderer

_STYLES_PATH = Path(__file__).parent / "styles.css"

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
            md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
            # Strip any raw HTML tags the model may have emitted (e.g. <div align="center">).
            # With html=False these would render as visible escaped text in the PDF.
            clean_markdown = re.sub(r'<[^>]+>', '', markdown)
            body_html = md.render(clean_markdown)

            # Build HTML shell using str.replace to avoid .format() KeyError on CSS with {}.
            html_string = (
                _HTML_TEMPLATE
                .replace("{css}", css)
                .replace("{body}", body_html)
            )

            # Create output directory and write PDF.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            weasyprint.HTML(string=html_string).write_pdf(str(output_path))

        await asyncio.to_thread(_write_pdf)
