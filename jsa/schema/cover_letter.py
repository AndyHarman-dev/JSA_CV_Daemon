"""Structured cover-letter schema — tolerant by design.

A CoverLetter is an optional salutation, one or more body paragraphs, and an optional
sign-off. Because it is a **separate schema** from CVDocument and the serializer only
renders known fields, a cover letter can never share a payload with a CV and stray keys
cannot leak into the output. Like the CV schema, this is tolerant: ``extra="ignore"`` and
a ``mode="before"`` normalizer accept the field-name and shape variants a model emits
(``body``/``content`` for paragraphs, a single string instead of a list, ``greeting``/
``closing`` for the salutation/sign-off).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Mirrors the old MIN_CL_CHARS floor: a real letter is more than a one-line note.
MIN_COVER_LETTER_CHARS = 150


def _as_paragraphs(value: Any) -> list[str]:
    """Coerce paragraphs into a list of strings; split a blob on blank lines."""
    if isinstance(value, str):
        parts = re.split(r"\n\s*\n", value.strip())
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for x in value:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
            elif isinstance(x, dict):
                # e.g. {"text": "..."} paragraph objects
                t = x.get("text") or x.get("content")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        return out
    return []


class CoverLetter(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    salutation: str | None = None  # e.g. "Dear Hiring Manager,"
    paragraphs: list[str] = Field(min_length=1)
    signoff: str | None = None  # e.g. "Sincerely,\nJane Doe"

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        out: dict[str, Any] = {}
        for k in ("salutation", "greeting", "salut", "dear"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                out["salutation"] = v.strip()
                break
        for k in ("signoff", "sign_off", "closing", "signature", "valediction"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                out["signoff"] = v.strip()
                break
        paragraphs: list[str] = []
        for k in ("paragraphs", "body", "content", "text", "letter", "paragraph"):
            if k in d:
                paragraphs = _as_paragraphs(d[k])
                if paragraphs:
                    break
        out["paragraphs"] = paragraphs
        return out

    @field_validator("paragraphs")
    @classmethod
    def _paragraphs_nonempty(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("cover letter must have at least one non-empty paragraph")
        return cleaned

    @model_validator(mode="after")
    def _min_length(self) -> "CoverLetter":
        total = sum(len(p) for p in self.paragraphs)
        if total < MIN_COVER_LETTER_CHARS:
            raise ValueError(
                f"cover letter body is too short ({total} chars; minimum "
                f"{MIN_COVER_LETTER_CHARS}) — looks like a summary, not the letter"
            )
        return self
