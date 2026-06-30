"""Structured-output schemas for pipeline FINAL payloads.

The cv_adjust / cover_letter stages emit JSON conforming to these Pydantic models
instead of free-form Markdown. Schema validation (in jsa/pipeline/stages.py) replaces
the old regex heuristics: a change-log, summary, or mixed CV/CL payload cannot satisfy
the schema, so it is rejected and fed into the self-heal loop. The validated object is
then serialized to canonical Markdown by jsa/render/serialize.py.
"""

from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import Contact, CVDocument, Entry, Section, cv_has_summary

__all__ = [
    "CoverLetter",
    "Contact",
    "CVDocument",
    "Section",
    "Entry",
    "cv_has_summary",
]
