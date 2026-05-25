"""WeasyPrintRenderer: Markdown -> HTML -> PDF via WeasyPrint."""

import asyncio
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
        # Read CSS at render time so user edits take effect without restart.
        css = _STYLES_PATH.read_text(encoding="utf-8")

        # Convert Markdown -> HTML with table and strikethrough extensions.
        md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
        body_html = md.render(markdown)

        html_string = _HTML_TEMPLATE.format(css=css, body=body_html)

        # Ensure destination directory exists (sync — fast, no I/O worth threading).
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # WeasyPrint is sync and CPU-bound; run in a thread to avoid blocking the loop.
        def _write_pdf() -> None:
            import weasyprint  # local import keeps the module load-time cost zero
            weasyprint.HTML(string=html_string).write_pdf(str(output_path))

        await asyncio.to_thread(_write_pdf)
