"""Structured CV schema — tolerant by design.

A CVDocument is a contact block plus an ordered list of sections. Each section is a
single **uniform** shape (a name + optional free text + optional flat items + optional
sub-entries) rather than a discriminated union of rigid typed sections.

Why uniform/tolerant instead of a tagged union:
    A live run showed the model reliably produces *good CV content* but will not reliably
    emit a machine-oriented tagged union. It dropped the ``type`` discriminator on every
    section (→ ``union_tag_not_found``), used ``title`` and ``name`` interchangeably, and
    invented content keys (``content``/``items``/``subsections``) — and held those shapes
    through two self-heal corrections. The model has a strong, sensible prior: a CV section
    is "a named block with content." So the schema meets that prior instead of fighting it.

Robustness model:
    - ``extra="ignore"`` (not ``forbid``): stray/invented keys are dropped, not fatal.
    - A ``mode="before"`` normalizer classifies content **by Python type**, not by exact
      key name — a list of strings becomes ``items``, a list of objects becomes ``entries``,
      a string becomes ``text`` — so whatever key the model picks from a generous-but-bounded
      set lands in the right place.

Contamination defense — what the schema gate covers, and where it stops:
    Stray keys (change-log, commentary) can't leak because the **serializer only renders
    known fields**, and the CV/cover-letter use **separate per-stage schemas** so they
    can't share a payload. But separate schemas only stop the two stages *sharing* a
    payload — they do NOT stop the model from emitting the wrong *content-kind* into the
    right *shape*. A live run proved this: at the cv_adjust stage the model spontaneously
    wrote a cover letter (one nameless section of prose paragraphs), which satisfied
    "contact + ≥1 section" and shipped a letter labelled "CV". So a **content-kind guard**
    (`_not_a_cover_letter`) is a hard gate here: letter formulas in the CV text reject the
    payload (→ self-heal → fail if uncorrected). A letter-as-CV is *corruption*, not
    thinness, so failing the job is the right outcome.

    The hard gates are therefore: contact ``name`` + ≥1 renderable section + not-a-letter.
    Everything else stays permissive — a false reject kills a job, a lenient accept only
    renders a slightly-thin CV the user still reviews. (A *missing summary* is thinness, not
    corruption, so it is NOT gated here — it is nudged in the self-heal loop and tolerated
    if uncorrected; see jsa/pipeline/stages.py::_self_heal_final.)
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Loose(BaseModel):
    # Tolerant base: ignore unknown keys (no contamination risk — the serializer renders
    # only known fields) and accept population by field name.
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --- helpers ---------------------------------------------------------------------------

# A section's content can arrive under any key the model invents. Rather than an
# allowlist of content keys (which silently *drops* content under an unanticipated key —
# an invisible CV-thinning bug), we use a denylist of "meta" keys and absorb everything
# else, classifying by value *type* (str→text, list[str]→items, list[dict]→entries).
# Over-absorbing a stray label is a visible, rare nuisance; silently losing real content
# is invisible and reintroduces the content-fidelity problem this whole change fixes.
_SECTION_META_KEYS = frozenset({
    "name", "title", "heading", "section", "section_name", "section_title",
    "label", "type", "kind", "id", "order", "index", "icon",
})

_HEADING_KEYS = ("heading", "title", "role", "position", "name", "degree", "project", "label")
_SUBHEADING_KEYS = ("subheading", "subtitle", "company", "organization", "employer",
                    "institution", "school", "issuer")
_DATE_KEYS = ("dates", "date", "period", "duration", "when", "years", "year")
_LOCATION_KEYS = ("location", "place", "city")
_TEXT_KEYS = ("text", "description", "summary", "content", "detail")
_BULLET_KEYS = ("bullets", "points", "highlights", "achievements", "responsibilities",
                "items", "details", "tasks", "list")
_NAME_KEYS = ("name", "title", "heading", "section", "section_name", "label")

# Entry-level meta keys to skip when absorbing leftover content (the entry analogue of
# _SECTION_META_KEYS). Everything not consumed as a known field and not meta is absorbed —
# URLs → links, other strings → text, lists → bullets — so a `url`/`stack`/`technologies`
# key the model invents is never silently dropped.
_ENTRY_META_KEYS = frozenset({"type", "kind", "id", "order", "index", "icon"})

# Matches an explicit link key or a URL-shaped string (github.com/..., https://..., www…).
_LINK_KEYS = frozenset({"url", "link", "links", "repo", "repository", "github",
                        "gitlab", "href", "website", "homepage", "demo", "live"})
_URL_RE = re.compile(r"(https?://|www\.|[\w-]+\.(?:com|org|io|dev|net|app|gg|me|co|ai)\b)", re.I)

# Cover-letter "tells": structural formulas that belong in a letter, never a CV. Used by the
# content-kind guard to reject a payload where the model wrote a cover letter into the CV
# shape (an observed live failure). Deliberately excludes sentiment words ("passionate
# about") that legitimately appear in CV summaries — only formulaic letter idioms. A single
# accidental match is tolerated; two or more is decisive (the observed failure matched three).
_LETTER_FORMULA_RE = re.compile(
    "|".join([
        r"writing to express",
        r"express my (?:strong |sincere |keen )?interest",
        r"would welcome (?:the opportunity|discussing|the chance)",
        r"ready to contribute immediately",
        r"\bdear hiring\b",
        r"\bdear (?:sir|madam|mr|ms|mrs)\b",
        r"\bsincerely,",
        r"\byours (?:sincerely|faithfully|truly)\b",
        r"thank you for (?:your )?consider",
        r"look forward to (?:hearing|discussing|speaking)",
        r"\bi am (?:writing|applying) (?:to|for)\b",
        r"\bi'?m applying for\b",
    ]),
    re.I,
)
_LETTER_MATCH_THRESHOLD = 2

# Names that mark a summary/profile section. Used by the (soft) summary nudge in the
# self-heal loop via cv_has_summary() — NOT a hard schema gate (a missing summary is
# thinness, not corruption).
SUMMARY_NAME_RE = re.compile(r"\b(summary|profile|objective|about|overview)\b", re.I)


def _first_str(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value among ``keys`` (in order)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _str_list(value: Any) -> list[str]:
    """Coerce a value into a list of non-empty strings (drops non-string members)."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [x.strip() for x in value if isinstance(x, str) and x.strip()]
    return []


# --- models ----------------------------------------------------------------------------


class Contact(_Loose):
    """Candidate identity + contact channels. Only ``name`` is required."""

    name: str = Field(validation_alias="name")
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)  # linkedin / github / portfolio URLs

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        out: dict[str, Any] = {}
        name = _first_str(d, ("name", "full_name", "fullName", "fullname"))
        if name is not None:
            out["name"] = name
        for canonical, keys in (
            ("email", ("email", "e-mail", "mail")),
            ("phone", ("phone", "telephone", "tel", "mobile")),
            ("location", ("location", "address", "city")),
        ):
            v = _first_str(d, keys)
            if v is not None:
                out[canonical] = v
        links: list[str] = []
        for k in ("links", "urls", "websites", "profiles", "social"):
            links.extend(_str_list(d.get(k)))
        # Common single-link fields.
        for k in ("linkedin", "github", "portfolio", "website", "url"):
            links.extend(_str_list(d.get(k)))
        if links:
            out["links"] = links
        return out

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("contact.name must be non-empty")
        return v.strip()


class Entry(_Loose):
    """A uniform sub-entry: one job, degree, project, certification, etc.

    All fields optional. The serializer renders whichever are present:
    ``**heading** — subheading`` / ``*dates | location*`` / text paragraph / ``- bullets``.
    """

    heading: str | None = None       # role / project / degree / award title
    subheading: str | None = None    # company / institution / issuer
    dates: str | None = None
    location: str | None = None
    text: str | None = None          # a prose description for the entry
    bullets: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)  # repo / demo / portfolio URLs

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, str):  # a bare string entry → its text
            return {"text": data.strip()} if data.strip() else {}
        if not isinstance(data, dict):
            return data
        d = dict(data)
        out: dict[str, Any] = {}
        used: set[str] = set(k for k in d if k in _ENTRY_META_KEYS)
        for canonical, keys in (
            ("heading", _HEADING_KEYS),
            ("subheading", _SUBHEADING_KEYS),
            ("dates", _DATE_KEYS),
            ("location", _LOCATION_KEYS),
            ("text", _TEXT_KEYS),
        ):
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    out[canonical] = v.strip()
                    used.add(k)
                    break
        # Absorb every leftover value so nothing is silently dropped (the bug that lost
        # per-project `url` keys): URL-shaped strings → links, other strings → extra text,
        # lists → bullets (with URL members split off into links).
        bullets: list[str] = []
        links: list[str] = []
        extra_text: list[str] = []
        for k, v in d.items():
            if k in used:
                continue
            if isinstance(v, list):
                for x in _str_list(v):
                    (links if _URL_RE.search(x) else bullets).append(x)
            elif isinstance(v, str) and v.strip():
                s = v.strip()
                if k in _LINK_KEYS or _URL_RE.search(s):
                    links.append(s)
                else:
                    extra_text.append(s)
        if extra_text:
            out["text"] = "\n\n".join(([out["text"]] if out.get("text") else []) + extra_text)
        if bullets:
            out["bullets"] = bullets
        if links:
            out["links"] = links
        return out

    # No "must have content" guard: with denylist absorption a stray object (e.g. a
    # `metadata: {…}` key) can normalize to an empty entry. Failing validation there would
    # false-reject the whole CV; instead an empty entry is harmless — the serializer renders
    # nothing for it (see jsa/render/serialize.py::_entry_block).


class Section(_Loose):
    """A uniform CV section: a name plus content in any of three shapes.

    Content is classified by type: free-text prose (``text``), a flat keyword/skill list
    (``items``), and/or structured sub-entries (``entries``). A section may populate more
    than one (e.g. an intro paragraph followed by entries).
    """

    name: str = ""
    text: str | None = None
    items: list[str] = Field(default_factory=list)
    entries: list[Entry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _classify(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        name = _first_str(d, _NAME_KEYS) or ""
        text_parts: list[str] = []
        items: list[str] = []
        entries: list[Any] = []
        for key, val in d.items():
            if key in _SECTION_META_KEYS:
                continue
            if isinstance(val, str):
                if val.strip():
                    text_parts.append(val.strip())
            elif isinstance(val, dict):
                entries.append(val)
            elif isinstance(val, list):
                if any(isinstance(x, dict) for x in val):
                    entries.extend(x for x in val if isinstance(x, dict))
                    # stray strings in a mixed list become items
                    items.extend(x.strip() for x in val if isinstance(x, str) and x.strip())
                else:
                    items.extend(_str_list(val))
        out: dict[str, Any] = {"name": name}
        if text_parts:
            out["text"] = "\n\n".join(text_parts)
        if items:
            out["items"] = items
        if entries:
            out["entries"] = entries
        return out


def _section_has_content(s: Section) -> bool:
    if s.text or s.items:
        return True
    return any(
        (e.heading or e.subheading or e.text or e.bullets or e.links) for e in s.entries
    )


def _cv_text_blob(sections: list[Section]) -> str:
    """All human-readable strings in the CV, for content-kind heuristics."""
    parts: list[str] = []
    for s in sections:
        if s.name:
            parts.append(s.name)
        if s.text:
            parts.append(s.text)
        parts.extend(s.items)
        for e in s.entries:
            for v in (e.heading, e.subheading, e.text):
                if v:
                    parts.append(v)
            parts.extend(e.bullets)
    return "\n".join(parts)


def cv_has_summary(cv: "CVDocument") -> bool:
    """True if the CV carries a summary/profile section with prose or items.

    Used by the self-heal loop for a *soft* nudge (see jsa/pipeline/stages.py); a missing
    summary is deliberately NOT a hard validation failure.
    """
    return any(
        SUMMARY_NAME_RE.search(s.name or "") and (s.text or s.items) for s in cv.sections
    )


class CVDocument(_Loose):
    """A complete, structured CV. The deliverable of the cv_adjust stage."""

    contact: Contact
    sections: list[Section] = Field(min_length=1)

    @model_validator(mode="after")
    def _has_renderable_content(self) -> "CVDocument":
        # The CV-vs-noise gate lives here, at the document level: contact.name (required on
        # Contact) plus at least one section that actually renders something. An individual
        # empty/odd section is tolerated (the serializer skips it) so it can't false-reject
        # an otherwise-good CV — but a payload with no renderable content at all is rejected.
        if not any(_section_has_content(s) for s in self.sections):
            raise ValueError("CV has no renderable section content")
        return self

    @model_validator(mode="after")
    def _not_a_cover_letter(self) -> "CVDocument":
        # Content-kind guard: the cv_adjust stage must produce a résumé, not a letter. The
        # model has been observed to emit cover-letter prose into the CV shape; separate
        # per-stage schemas don't catch that (the letter satisfies "contact + ≥1 section").
        # Letter formulas (not sentiment words) in the CV text are the tell. Two+ matches is
        # decisive → reject → self-heal → fail if uncorrected (corruption, not thinness).
        hits = _LETTER_FORMULA_RE.findall(_cv_text_blob(self.sections))
        if len(hits) >= _LETTER_MATCH_THRESHOLD:
            sample = ", ".join(sorted({h.lower() for h in hits})[:3])
            raise ValueError(
                f"this reads as a cover letter, not a CV (letter phrasing: {sample}). The "
                "cv_adjust output must be a résumé — a contact block plus sections such as "
                "Summary, Experience, Skills, and Education with bullet points — not a "
                "letter addressed to an employer. Re-emit the candidate's CV as structured "
                "JSON; put any motivation prose in the cover-letter stage, not here."
            )
        return self
