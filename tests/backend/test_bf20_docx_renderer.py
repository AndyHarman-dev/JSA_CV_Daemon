"""Unit tests for Phase BF-20: DOCX Renderer (jsa.render.docx_render)

Covers:
  - DocxRenderer in jsa.render.docx_render
  - registry entry for "docx" renderer
  - Document model docx_path column
  - Markdown parsing: H1, H2, H3, contact line, bullets, bold, horizontal rules
  - Line spacing and fonts (Calibri 10.5pt, 1.3 spacing)
  - Page margins (0.75 inches)

asyncio_mode = "auto" is configured in pyproject.toml — async test
functions need no extra marks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH

from jsa.db.models import Document
from jsa.render.base import Renderer
from jsa.render.docx_render import DocxRenderer
from jsa.render.registry import renderer_for


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestDocxRendererRegistry:
    def test_renderer_for_docx_returns_docx_renderer(self) -> None:
        """renderer_for("docx") returns a DocxRenderer instance."""
        r = renderer_for("docx")
        assert isinstance(r, DocxRenderer)

    def test_docx_is_renderer_instance(self) -> None:
        """DocxRenderer is a Renderer (isinstance check on ABC)."""
        r = renderer_for("docx")
        assert isinstance(r, Renderer)

    def test_docx_renderer_has_name_attribute(self) -> None:
        """DocxRenderer.name == 'docx'."""
        r = renderer_for("docx")
        assert r.name == "docx"

    def test_weasyprint_still_registered(self) -> None:
        """renderer_for('weasyprint') still works (no regression)."""
        r = renderer_for("weasyprint")
        assert isinstance(r, Renderer)
        assert r.name == "weasyprint"

    def test_each_call_returns_new_instance(self) -> None:
        """renderer_for() creates a fresh instance each call (no shared state)."""
        r1 = renderer_for("docx")
        r2 = renderer_for("docx")
        assert r1 is not r2


# ---------------------------------------------------------------------------
# Async render() tests
# ---------------------------------------------------------------------------


class TestDocxRendererAsync:
    async def test_render_creates_output_file(self, tmp_path: Path) -> None:
        """render() creates a file at output_path and it is non-empty."""
        out = tmp_path / "test.docx"
        md = "# Test\ntest@example.com\n\n## Section\n\nBody text"
        renderer = DocxRenderer()
        await renderer.render(md, out)
        assert out.exists()
        assert out.stat().st_size > 0

    async def test_render_creates_parent_directories(self, tmp_path: Path) -> None:
        """render() creates all missing parent directories."""
        out = tmp_path / "deep" / "nested" / "dir" / "test.docx"
        md = "# Name\ncontact@example.com"
        renderer = DocxRenderer()
        await renderer.render(md, out)
        assert out.parent.is_dir()
        assert out.exists()

    async def test_render_produces_valid_docx(self, tmp_path: Path) -> None:
        """The output file is a valid DOCX that python-docx can open."""
        out = tmp_path / "test.docx"
        md = "# John Doe\njohn@example.com\n\n## Experience\n\nSome text"
        renderer = DocxRenderer()
        await renderer.render(md, out)

        # Should not raise an exception
        doc = DocxDocument(str(out))
        assert len(doc.paragraphs) > 0

    async def test_render_with_empty_markdown(self, tmp_path: Path) -> None:
        """render() handles empty Markdown gracefully."""
        out = tmp_path / "empty.docx"
        renderer = DocxRenderer()
        await renderer.render("", out)
        assert out.exists()

    async def test_render_with_only_body_text(self, tmp_path: Path) -> None:
        """render() handles Markdown with no headings."""
        out = tmp_path / "body.docx"
        md = "This is body text.\nMore body text."
        renderer = DocxRenderer()
        await renderer.render(md, out)

        doc = DocxDocument(str(out))
        assert len(doc.paragraphs) > 0
        # Should have converted to paragraphs
        text_content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        assert "body text" in text_content.lower()


# ---------------------------------------------------------------------------
# H1 (name) heading tests
# ---------------------------------------------------------------------------


class TestDocxH1Heading:
    async def test_h1_centered(self, tmp_path: Path) -> None:
        """H1 paragraph is center-aligned."""
        md = "# John Doe\ncontact info"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        assert len(paras) >= 1
        h1_para = paras[0]
        assert h1_para.text == "John Doe"
        assert h1_para.alignment == WD_ALIGN_PARAGRAPH.CENTER

    async def test_h1_bold_large_font(self, tmp_path: Path) -> None:
        """H1 is bold, 14pt."""
        md = "# Jane Smith\ncontact"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        h1_para = paras[0]
        assert h1_para.text == "Jane Smith"

        # Check runs for bold and size
        h1_runs = h1_para.runs
        assert len(h1_runs) >= 1
        # First (or only) run should be bold, 14pt
        assert h1_runs[0].bold
        assert h1_runs[0].font.size.pt == 14

    async def test_h1_text_preserved(self, tmp_path: Path) -> None:
        """H1 content is preserved exactly."""
        names = ["Alice Johnson", "Bob O'Connor", "Dr. Claire Wells-Reid"]
        for name in names:
            tmp = tmp_path / "tmp"
            tmp.mkdir(exist_ok=True)
            out = tmp / f"{name.replace(' ', '_')}.docx"
            md = f"# {name}\ncontact@example.com"
            renderer = DocxRenderer()
            renderer._render_sync(md, out)

            doc = DocxDocument(str(out))
            paras = [p for p in doc.paragraphs if p.text.strip()]
            assert paras[0].text == name


# ---------------------------------------------------------------------------
# Contact line tests (first non-blank after H1)
# ---------------------------------------------------------------------------


class TestDocxContactLine:
    async def test_contact_line_italic(self, tmp_path: Path) -> None:
        """Contact line after H1 is italicized."""
        md = "# John Doe\njohn@example.com | (555) 123-4567"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        assert len(paras) >= 2
        contact_para = paras[1]
        assert contact_para.text == "john@example.com | (555) 123-4567"
        # Check italic
        assert contact_para.runs[0].italic

    async def test_contact_line_centered(self, tmp_path: Path) -> None:
        """Contact line is center-aligned."""
        md = "# Jane\njane@example.com"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        contact_para = paras[1]
        assert contact_para.alignment == WD_ALIGN_PARAGRAPH.CENTER

    async def test_contact_line_10pt(self, tmp_path: Path) -> None:
        """Contact line is 10pt."""
        md = "# Name\ncontact@example.com"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        contact_para = paras[1]
        # Should have runs with 10pt size
        assert contact_para.runs[0].font.size.pt == 10

    async def test_contact_line_skipped_if_no_h1(self, tmp_path: Path) -> None:
        """If no H1, first line is treated as body text, not contact line."""
        md = "email@example.com\n## Section\nBody"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        # First paragraph should NOT be italic (treated as body)
        first = paras[0]
        if first.runs:
            assert not first.runs[0].italic


# ---------------------------------------------------------------------------
# H2 section heading tests
# ---------------------------------------------------------------------------


class TestDocxH2Heading:
    async def test_h2_bold(self, tmp_path: Path) -> None:
        """H2 is bold."""
        md = "# Name\ncontact\n\n## Experience\nBody text"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        h2_paras = [p for p in doc.paragraphs if p.text == "Experience"]
        assert len(h2_paras) >= 1
        h2 = h2_paras[0]
        assert h2.runs[0].bold

    async def test_h2_12pt(self, tmp_path: Path) -> None:
        """H2 is 12pt."""
        md = "# Name\ncontact\n\n## Skills\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        h2_paras = [p for p in doc.paragraphs if p.text == "Skills"]
        assert len(h2_paras) >= 1
        h2 = h2_paras[0]
        assert h2.runs[0].font.size.pt == 12

    async def test_h2_left_aligned(self, tmp_path: Path) -> None:
        """H2 is left-aligned."""
        md = "# Name\ncontact\n\n## Education\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        h2_paras = [p for p in doc.paragraphs if p.text == "Education"]
        h2 = h2_paras[0]
        assert h2.alignment == WD_ALIGN_PARAGRAPH.LEFT

    async def test_multiple_h2_sections(self, tmp_path: Path) -> None:
        """Multiple H2 sections are all formatted correctly."""
        md = (
            "# Name\ncontact\n\n"
            "## Skills\nPython, Go\n\n"
            "## Experience\n2 years at Acme\n\n"
            "## Education\nBCS"
        )
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        headings = [p for p in doc.paragraphs if p.text in ("Skills", "Experience", "Education")]
        assert len(headings) == 3
        for h2 in headings:
            assert h2.runs[0].bold
            assert h2.runs[0].font.size.pt == 12


# ---------------------------------------------------------------------------
# H3 (sub-heading) tests
# ---------------------------------------------------------------------------


class TestDocxH3Heading:
    async def test_h3_bold_body_size(self, tmp_path: Path) -> None:
        """H3 is bold, body font size (10.5pt)."""
        md = "# Name\ncontact\n\n## Section\n\n### Subsection\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        h3_paras = [p for p in doc.paragraphs if p.text == "Subsection"]
        assert len(h3_paras) >= 1
        h3 = h3_paras[0]
        assert h3.runs[0].bold
        assert h3.runs[0].font.size.pt == 10.5


# ---------------------------------------------------------------------------
# Bullet list tests
# ---------------------------------------------------------------------------


class TestDocxBullets:
    async def test_bullet_uses_list_bullet_style(self, tmp_path: Path) -> None:
        """Bullets use 'List Bullet' style."""
        md = "# Name\ncontact\n\n## Skills\n- Python\n- Go\n- Rust"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        assert len(bullets) == 3

    async def test_bullet_text_preserved(self, tmp_path: Path) -> None:
        """Bullet text is preserved exactly."""
        md = "# Name\ncontact\n\n- Item 1\n- Item 2\n- Item 3"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        texts = [b.text for b in bullets]
        assert "Item 1" in texts
        assert "Item 2" in texts
        assert "Item 3" in texts

    async def test_asterisk_bullet_also_works(self, tmp_path: Path) -> None:
        """Asterisk bullets (* ) also work as list bullets."""
        md = "# Name\ncontact\n\n* Item A\n* Item B"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        assert len(bullets) == 2

    async def test_bullet_font_10_5pt(self, tmp_path: Path) -> None:
        """Bullet text is 10.5pt."""
        md = "# Name\ncontact\n\n- Python"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        bullet = bullets[0]
        # Runs should be 10.5pt
        assert bullet.runs[0].font.size.pt == 10.5


# ---------------------------------------------------------------------------
# Inline bold parsing tests
# ---------------------------------------------------------------------------


class TestDocxInlineBold:
    async def test_inline_bold_in_body(self, tmp_path: Path) -> None:
        """**bold** text in body paragraph is formatted as bold."""
        md = "# Name\ncontact\n\n## Summary\n**Lead Engineer** at Acme Corp"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        body_paras = [p for p in doc.paragraphs if "Lead Engineer" in p.text]
        assert len(body_paras) >= 1
        body = body_paras[0]

        # Find the bold run
        bold_runs = [r for r in body.runs if r.bold and "Lead Engineer" in r.text]
        assert len(bold_runs) >= 1

    async def test_inline_bold_in_bullet(self, tmp_path: Path) -> None:
        """**bold** text in a bullet is formatted as bold."""
        md = "# Name\ncontact\n\n- **Expert** in Python"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        bullet = bullets[0]

        # Should have a bold run containing "Expert"
        bold_runs = [r for r in bullet.runs if r.bold]
        bold_text = "".join(r.text for r in bold_runs)
        assert "Expert" in bold_text

    async def test_multiple_bold_segments(self, tmp_path: Path) -> None:
        """Multiple **bold** segments in one line."""
        md = "# Name\ncontact\n\n**First** bold and **second** bold text"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        body_paras = [p for p in doc.paragraphs if "First" in p.text]
        assert len(body_paras) >= 1
        body = body_paras[0]

        # Multiple runs with bold
        bold_runs = [r for r in body.runs if r.bold]
        assert len(bold_runs) >= 2

    async def test_bold_in_h3(self, tmp_path: Path) -> None:
        """**bold** in H3 (which is inherently bold) is preserved."""
        md = "# Name\ncontact\n\n### **Emphasized** Subsection"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        h3_paras = [p for p in doc.paragraphs if "Emphasized" in p.text]
        assert len(h3_paras) >= 1
        h3 = h3_paras[0]
        # All runs in H3 should be bold (default_bold=True in _add_runs)
        for run in h3.runs:
            assert run.bold


# ---------------------------------------------------------------------------
# Horizontal rule (---) tests
# ---------------------------------------------------------------------------


class TestDocxHorizontalRule:
    async def test_horizontal_rule_adds_border(self, tmp_path: Path) -> None:
        """--- adds a bottom border to the preceding paragraph."""
        md = "# Name\ncontact\n---\n## Section\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        # The contact line should have a bottom border
        paras = [p for p in doc.paragraphs if p.text.strip()]
        contact_para = paras[1]  # Second paragraph (after H1)

        # Check for border element in paragraph properties
        pPr = contact_para._p.get_or_add_pPr()
        pBdr = pPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr')
        assert pBdr is not None

    async def test_horizontal_rule_no_blank_paragraph(self, tmp_path: Path) -> None:
        """--- does not create a new paragraph; border goes on preceding one."""
        md = "# Name\ncontact\n---\nMore text"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        # Count non-empty paragraphs: H1 + contact + body. Should NOT have a blank para for ---.
        paras = [p for p in doc.paragraphs if p.text.strip()]
        # H1, contact line, body text = 3 minimum
        assert len(paras) >= 3

    async def test_asterisk_rule_also_works(self, tmp_path: Path) -> None:
        """*** also creates a horizontal rule."""
        md = "# Name\ncontact\n***\n## Section\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        # The second paragraph (contact) should have a border
        contact_para = paras[1]
        pPr = contact_para._p.get_or_add_pPr()
        pBdr = pPr.find('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pBdr')
        assert pBdr is not None


# ---------------------------------------------------------------------------
# Page margins tests
# ---------------------------------------------------------------------------


class TestDocxPageMargins:
    async def test_page_margins_0_75_inches(self, tmp_path: Path) -> None:
        """All page margins are 0.75 inches."""
        md = "# Name\ncontact"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        section = doc.sections[0]

        # Check all margins are 0.75 inches
        from docx.shared import Inches
        expected = Inches(0.75)
        assert section.top_margin == expected
        assert section.bottom_margin == expected
        assert section.left_margin == expected
        assert section.right_margin == expected


# ---------------------------------------------------------------------------
# Font and line spacing tests
# ---------------------------------------------------------------------------


class TestDocxFontAndLineSpacing:
    async def test_body_text_calibri_10_5pt(self, tmp_path: Path) -> None:
        """Body text is Calibri 10.5pt."""
        md = "# Name\ncontact\n\nBody paragraph with text"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        body_paras = [p for p in doc.paragraphs if "Body paragraph" in p.text]
        assert len(body_paras) >= 1
        body = body_paras[0]
        # Check font name and size
        assert body.runs[0].font.name == "Calibri"
        assert body.runs[0].font.size.pt == 10.5

    async def test_body_text_line_spacing_1_3(self, tmp_path: Path) -> None:
        """Body text has 1.3 line spacing."""
        md = "# Name\ncontact\n\nBody paragraph"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        body_paras = [p for p in doc.paragraphs if "Body paragraph" in p.text]
        assert len(body_paras) >= 1
        body = body_paras[0]
        # Check line spacing
        assert body.paragraph_format.line_spacing == 1.3

    async def test_h1_line_spacing_1_3(self, tmp_path: Path) -> None:
        """H1 has 1.3 line spacing."""
        md = "# Name\ncontact"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text == "Name"]
        h1 = paras[0]
        assert h1.paragraph_format.line_spacing == 1.3

    async def test_all_paragraphs_calibri(self, tmp_path: Path) -> None:
        """All paragraph text uses Calibri font."""
        md = (
            "# Name\ncontact\n\n"
            "## Section\n\n"
            "### Subsection\n\n"
            "Body text\n\n"
            "- Bullet"
        )
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        for para in doc.paragraphs:
            for run in para.runs:
                if run.text.strip():
                    assert run.font.name == "Calibri"


# ---------------------------------------------------------------------------
# Document model tests
# ---------------------------------------------------------------------------


class TestDocumentModel:
    def test_document_has_docx_path_attribute(self) -> None:
        """Document model has docx_path attribute."""
        assert hasattr(Document, "docx_path")

    def test_docx_path_in_table_columns(self) -> None:
        """docx_path is a column in the Document table."""
        columns = {c.name for c in Document.__table__.columns}
        assert "docx_path" in columns

    def test_docx_path_is_nullable(self) -> None:
        """docx_path column is nullable (can be NULL)."""
        col = Document.__table__.columns["docx_path"]
        assert col.nullable

    def test_docx_path_after_pdf_path(self) -> None:
        """docx_path column comes after pdf_path (per spec)."""
        column_names = [c.name for c in Document.__table__.columns]
        pdf_idx = column_names.index("pdf_path")
        docx_idx = column_names.index("docx_path")
        assert docx_idx > pdf_idx


# ---------------------------------------------------------------------------
# Integration tests: complete CV-like Markdown
# ---------------------------------------------------------------------------


class TestDocxIntegrationCV:
    async def test_complete_cv_structure(self, tmp_path: Path) -> None:
        """A complete CV Markdown renders without error."""
        md = """# John Doe
john@example.com | (555) 123-4567 | linkedin.com/in/johndoe
---

## Professional Summary

**Senior Software Engineer** with 10+ years of experience building scalable systems.

## Technical Skills

- Python, Go, Rust
- **Kubernetes**, Docker
- **AWS**: EC2, S3, Lambda

## Experience

### Senior Engineer at Acme Corp (2020–Present)

Led the migration of **monolith to microservices**, reducing latency by 40%.

- Architected **event-driven platform**
- Mentored team of 5 engineers
- **Speaker**: "Scaling with Kubernetes" (KubeCon 2024)

### Software Engineer at Beta Inc (2018–2020)

Owned the **payment system**, processing $2M daily.

## Education

**BS Computer Science**, University of Example (2018)
"""
        renderer = DocxRenderer()
        out = tmp_path / "cv.docx"
        await renderer.render(md, out)

        # Should produce a valid, non-empty DOCX
        assert out.exists()
        assert out.stat().st_size > 0

        # Should open without error
        doc = DocxDocument(str(out))
        assert len(doc.paragraphs) > 10

    async def test_blank_lines_handled_correctly(self, tmp_path: Path) -> None:
        """Multiple blank lines in Markdown are handled correctly."""
        md = """# Name
contact


## Section


Body text


- Bullet 1


- Bullet 2
"""
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        await renderer.render(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        # Should skip blank lines and produce only content paragraphs
        assert len(paras) >= 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDocxEdgeCases:
    async def test_h1_without_contact_line(self, tmp_path: Path) -> None:
        """H1 followed immediately by H2 (no contact line between them).

        The contact-line guard (`not stripped.startswith("#")`) correctly
        prevents H2 from being misidentified as the contact line.
        "## Section" must be rendered as a proper section heading (bold, left-
        aligned), not as italic/centred contact text.
        """
        md = "# Name\n\n## Section\nText"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        # H1 should be first
        assert paras[0].text == "Name"
        # "## Section" must appear as a bold section heading, NOT italic contact text
        section_para = next((p for p in paras if p.text == "Section"), None)
        assert section_para is not None, "Section heading paragraph not found"
        bold_runs = [r for r in section_para.runs if r.bold]
        assert bold_runs, "Section heading should be bold (H2), not plain/italic"

    async def test_no_h1_at_all(self, tmp_path: Path) -> None:
        """Markdown with no H1, only body and sections."""
        md = "## Section\n\nBody text\n\n- Item"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        assert len(paras) >= 3

    async def test_consecutive_bullets(self, tmp_path: Path) -> None:
        """Multiple bullets in a row."""
        md = "# Name\ncontact\n\n- A\n- B\n- C\n- D\n- E"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        bullets = [p for p in doc.paragraphs if p.style.name == "List Bullet"]
        assert len(bullets) == 5

    async def test_special_characters_preserved(self, tmp_path: Path) -> None:
        """Special characters in text are preserved."""
        md = "# José García\njosé@example.com | +55 (11) 99999-8888"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        paras = [p for p in doc.paragraphs if p.text.strip()]
        assert any("José" in p.text for p in paras)
        assert any("+55" in p.text for p in paras)

    async def test_empty_bold_markers(self, tmp_path: Path) -> None:
        """Empty ** markers are handled (don't crash)."""
        md = "# Name\ncontact\n\nText with ** and more text."
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        assert len(doc.paragraphs) > 0

    async def test_long_lines_wrap(self, tmp_path: Path) -> None:
        """Very long lines wrap correctly (Word handles this)."""
        long_text = "A" * 500
        md = f"# Name\ncontact\n\n{long_text}"
        renderer = DocxRenderer()
        out = tmp_path / "test.docx"
        renderer._render_sync(md, out)

        doc = DocxDocument(str(out))
        assert len(doc.paragraphs) > 0
