"""Wider text extraction for the CV-editor chat's read-only file attachments.

``.pdf``/``.docx`` delegate to ``jsa.ingest.cv_loader.load_cv`` UNCHANGED — that
function is test-pinned to raise ``ValueError`` for every other extension
(``tests/backend/test_ingest.py``), so this module widens ingest by adding a new,
separate entry point rather than editing it. Plain-text-ish formats are read as UTF-8
with lossy replacement (attachments are read-only context for one turn, never stored,
so a best-effort decode is an acceptable trade for never hard-failing on a stray
non-UTF-8 byte)."""

from __future__ import annotations

from pathlib import Path

from jsa.ingest.cv_loader import load_cv

_PDF_DOCX = {".pdf", ".docx"}
_PLAIN_TEXT = {".txt", ".md", ".json", ".rtf", ".csv"}


class UnsupportedSource(ValueError):
    """Raised for an attachment extension neither ``load_cv`` nor this module can
    extract text from (e.g. an image) — the API layer maps this to HTTP 422."""


def load_text_source(path: Path) -> str:
    """Extract plain text from ``path`` for use as read-only chat context.

    Blocking (PDF/DOCX parsing, disk IO) — callers must wrap this in
    ``asyncio.to_thread``, per CLAUDE.md's concurrency rule.
    """
    suffix = path.suffix.lower()
    if suffix in _PDF_DOCX:
        return load_cv(path)
    if suffix in _PLAIN_TEXT:
        return path.read_text(encoding="utf-8", errors="replace")
    accepted = sorted(_PDF_DOCX | _PLAIN_TEXT)
    raise UnsupportedSource(
        f"unsupported attachment format {suffix!r} — accepted: {', '.join(accepted)}"
    )


__all__ = ["UnsupportedSource", "load_text_source"]
