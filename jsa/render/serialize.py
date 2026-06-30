"""Deterministic schema -> canonical Markdown serialization.

The pipeline (not the LLM) owns 100% of layout: a validated CVDocument / CoverLetter
is rendered to a fixed Markdown shape that the existing WeasyPrint (PDF) and Docx
renderers already handle well. Pure functions — the same object always produces
byte-identical Markdown, which is what makes the output consistent run-to-run.

Sections are uniform (see jsa/schema/cv.py): each may carry free ``text``, a flat
``items`` list, and/or structured ``entries``. The serializer dispatches on which of
those are populated. ``cv.sections`` order is rendered verbatim — section order is the
user's call (curated in the Structure Editor, or as the model emitted it), never the
serializer's. The one layout policy the program still enforces regardless of what the
model emits:
  - a Skills/Technologies section is rendered compactly (one line per group, comma-
    joined) rather than one item per line, so it doesn't fill the page.

The CV Markdown matches the header/section format documented in
jsa/prompts/PROMPT_CDADJUST.md:
  - `# Full Name` then the contact line on the next line (no blank between)
  - a `---` horizontal rule immediately before every `## Section` heading
  - `**bold**` lead-ins, `- ` bullets, single-column, no tables/HTML
"""

from __future__ import annotations

import re

from jsa.schema import CoverLetter, CVDocument, Entry, Section

# Above this length a flat item reads as a sentence, not a keyword → render as bullets
# instead of a comma-joined line.
_ITEM_INLINE_MAX = 60

# Section-name heuristic (the program owns this layout decision, not the model).
_SKILLS_RE = re.compile(
    r"\b(skills?|technolog|competenc|tool|expertise|proficienc|tech\s*stack|stack)\b", re.I
)


def _join(parts: list[str | None], sep: str = " | ") -> str:
    """Join the non-empty parts with `sep`."""
    return sep.join(p.strip() for p in parts if p and p.strip())


def _md_link(url: str) -> str:
    """A clickable Markdown link; bare domains get an https:// href but display as-is."""
    u = url.strip()
    href = u if re.match(r"^https?://", u, re.I) else f"https://{u}"
    return f"[{u}]({href})"


def _contact_line(cv: CVDocument) -> str:
    c = cv.contact
    links = [_md_link(u) for u in c.links]
    return _join([c.email, c.phone, *links, c.location])


def _items_block(items: list[str]) -> str:
    """Flat list → a comma-joined keyword line, or bullets when items are long."""
    clean = [i.strip() for i in items if i and i.strip()]
    if not clean:
        return ""
    if any(len(i) > _ITEM_INLINE_MAX for i in clean):
        return "\n".join(f"- {i}" for i in clean)
    return ", ".join(clean)


def _entry_block(e: Entry) -> str:
    lines: list[str] = []
    head = _join(
        [f"**{e.heading.strip()}**" if e.heading and e.heading.strip() else None,
         e.subheading.strip() if e.subheading and e.subheading.strip() else None],
        sep=" — ",
    )
    if head:
        lines.append(head)
    meta = _join([e.dates, e.location])
    if meta:
        lines.append(f"*{meta}*")
    if e.text and e.text.strip():
        lines.append(e.text.strip())
    lines.extend(f"- {b.strip()}" for b in e.bullets if b and b.strip())
    if e.links:
        lines.append(_join([_md_link(u) for u in e.links], sep=" · "))
    return "\n".join(lines)


def _skills_block(section: Section) -> str:
    """Compact skills rendering: one line per group, `**Group:** a, b, c`.

    Collapses the verbose two-line-per-category default (bold heading + body) so a Skills
    section stays tight instead of filling the page. Handles whichever shape the model
    emitted — a flat ``items`` list, or ``entries`` where each is a category.
    """
    lines: list[str] = []
    if section.text and section.text.strip():
        lines.append(section.text.strip())
    if section.items:
        lines.append(", ".join(i.strip() for i in section.items if i and i.strip()))
    for e in section.entries:
        body = _join(
            [e.text.strip() if e.text and e.text.strip() else None,
             ", ".join(b.strip() for b in e.bullets if b and b.strip()) or None],
            sep=", ",
        )
        label = e.heading.strip() if e.heading and e.heading.strip() else None
        if label and body:
            lines.append(f"**{label}:** {body}")
        elif label:
            lines.append(f"**{label}**")
        elif body:
            lines.append(body)
    return "\n".join(l for l in lines if l)


def _section_body(section: Section) -> str:
    if _SKILLS_RE.search(section.name or ""):
        return _skills_block(section).strip()
    blocks: list[str] = []
    if section.text and section.text.strip():
        blocks.append(section.text.strip())
    if section.items:
        items = _items_block(section.items)
        if items:
            blocks.append(items)
    if section.entries:
        entry_blocks = [b for b in (_entry_block(e) for e in section.entries) if b]
        if entry_blocks:
            blocks.append("\n\n".join(entry_blocks))
    return "\n\n".join(blocks).strip()


def cv_to_markdown(cv: CVDocument) -> str:
    """Render a validated CVDocument to canonical Markdown."""
    parts: list[str] = [f"# {cv.contact.name.strip()}", _contact_line(cv)]
    out = "\n".join(p for p in parts if p)

    for section in cv.sections:
        body = _section_body(section)
        if not body:
            continue
        name = section.name.strip()
        heading = f"## {name}\n" if name else ""
        out += f"\n\n---\n{heading}{body}"

    return out.strip() + "\n"


def cover_letter_to_markdown(cl: CoverLetter) -> str:
    """Render a validated CoverLetter to canonical Markdown (plain prose)."""
    blocks: list[str] = []
    if cl.salutation and cl.salutation.strip():
        blocks.append(cl.salutation.strip())
    blocks.extend(p.strip() for p in cl.paragraphs)
    if cl.signoff and cl.signoff.strip():
        blocks.append(cl.signoff.strip())
    return "\n\n".join(blocks).strip() + "\n"
