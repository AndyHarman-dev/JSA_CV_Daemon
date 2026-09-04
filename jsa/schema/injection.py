"""Per-job prompt injection ("PROMPT_INJECTOR") — the single normalization point.

A job may carry three optional user-authored overrides, stored as one JSON object in
the nullable ``jobs.injection`` TEXT column:

===============  ==========================================================
``prefix``       inserted *before* the stage's system prompt
``postfix``      appended *after* the stage's system prompt
``first_msg``    appended to the initial user message of a fresh session
===============  ==========================================================

One column, not three, because the domain shape is "absent, or all three together" —
three separate columns would let a half-written triple exist.

**Normalization happens here and nowhere else.** ``PromptInjection.normalized()`` strips
every field and collapses an all-blank triple to ``None``. This matters for more than
tidiness: CLAUDE.md → "Prompt caching (HTTP API backends)" documents a cross-job
system-prefix invariant, and an injection that survives as ``{"prefix": " "}`` would drop
that job out of the shared prompt-cache entry forever while lighting the UI's syringe
icon for nothing. Read sites must call ``parse_injection`` and trust its result — do not
re-strip ad hoc.

Wire shape is snake_case (``first_msg``), matching every other DTO in this API
(``current_stage``, ``fit_reason``, ``suggested_replies``) — the design prototype's
``firstMsg`` would be the only camelCase field in the whole surface.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, ValidationError


class PromptInjection(BaseModel):
    """The three per-job prompt overrides. Doubles as the PUT request body."""

    model_config = ConfigDict(extra="forbid")

    prefix: str = ""
    postfix: str = ""
    first_msg: str = ""

    def normalized(self) -> "PromptInjection | None":
        """Strip each field; return None when nothing survives."""
        p, s, f = self.prefix.strip(), self.postfix.strip(), self.first_msg.strip()
        if not (p or s or f):
            return None
        return PromptInjection(prefix=p, postfix=s, first_msg=f)


def parse_injection(raw: str | None) -> PromptInjection | None:
    """``Job.injection`` column -> normalized model. Malformed JSON reads as ``None``.

    This function **must not raise**. The column is plain TEXT and a hand-edited or
    legacy row (bad JSON, a JSON array, the prototype's ``firstMsg`` key, a non-string
    field value) must degrade to "no injection" — never hard-fail every stage of that
    job. ``extra="forbid"`` means a wrong key is a ``ValidationError``, not a decode
    error, so both are caught here.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        parsed = PromptInjection.model_validate(data)
    except ValidationError:
        return None
    return parsed.normalized()
