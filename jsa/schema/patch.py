"""Working copies + applier for tool-driven revision patching.

Part of the revision-tool-use plan (Phase 1). ``CvWorkingCopy``/``ClWorkingCopy`` hold
a **mutable, plain-dict** projection of a validated ``CVDocument``/``CoverLetter`` plus
a mint-once id side-map, so ``jsa/pipeline/tool_loop.py`` (Phase 2) can apply a bounded
sequence of small edits (see ``jsa/agents/tool_spec.py`` for the tool vocabulary) without
re-running the full tolerant-schema normalization on every single operation. Validation
happens exactly once, in ``finalize()``, by handing the reconstructed document to the
SAME gate ``jsa/pipeline/stages.py::_handle_final`` uses
(``jsa.pipeline.validation._validate_final_content``) — so a patched revision and a
full-rewrite revision are held to identical content rules.

ID discipline (mint-once side map, not positional-on-demand): ids are minted when the
working copy loads and are never reused, even after the entry/section/paragraph they
named is removed — see ``_id_error`` below for the unknown-vs-stale distinction this
enables. This is what lets a multi-edit revision fit in one ``get_cv``/``get_letter``
read: an id captured before a later, unrelated removal still resolves correctly (the ID
stability property Phase 1's tests pin).

Every mutation op returns a JSON-serializable result dict, never raises for a bad
argument or reference — ``{"ok": True, ...}`` or ``{"ok": False, "error": {"code",
"message", "hint"?}}`` (codes: ``unknown_id``, ``stale_id``, ``bad_argument``,
``validation_failed``). ``budget_exhausted``/``not_executed`` are loop-level concerns
(Phase 2's ``tool_loop.py``), not applier concerns, and are never returned here.
"""

from __future__ import annotations

import json
from typing import Any

from jsa.db.models import Stage
from jsa.pipeline.validation import FinalContentError, _validate_final_content
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument, Entry, SUMMARY_NAME_RE

# --- shared result-shape + id-lookup helpers ------------------------------------------


def _err(code: str, message: str, hint: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    return {"ok": False, "error": error}


def _ok(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


def _id_error(an_id: str, retired: set[str], hint: str) -> dict[str, Any]:
    """A stale reference (once minted, since removed) is distinguished from an unknown
    one (never minted — a hallucinated id) so the model can tell "re-read, it moved"
    apart from "that id was never real"."""
    if an_id in retired:
        return _err("stale_id", f"id {an_id!r} no longer exists (it was removed)", hint)
    return _err("unknown_id", f"id {an_id!r} is not a known id", hint)


def _clean_str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def _entry_to_dict(entry: Entry) -> dict[str, Any]:
    return {
        "heading": entry.heading,
        "subheading": entry.subheading,
        "dates": entry.dates,
        "location": entry.location,
        "text": entry.text,
        "bullets": list(entry.bullets),
        "links": list(entry.links),
    }


def _clean_entry_input(entry: Any) -> dict[str, Any] | None:
    """Coerce a model-supplied entry object into working-copy shape, or ``None`` if
    ``entry`` isn't even an object. Strips ``id`` (defense in depth — the applier never
    trusts a model-supplied id; see the module docstring) and drops any field whose
    type doesn't match, rather than rejecting the whole entry, mirroring
    ``jsa/schema/cv.py``'s tolerant-absorption philosophy."""
    if not isinstance(entry, dict):
        return None
    d = {k: v for k, v in entry.items() if k != "id"}
    return {
        "heading": _clean_str_or_none(d.get("heading")),
        "subheading": _clean_str_or_none(d.get("subheading")),
        "dates": _clean_str_or_none(d.get("dates")),
        "location": _clean_str_or_none(d.get("location")),
        "text": _clean_str_or_none(d.get("text")),
        "bullets": _clean_str_list(d.get("bullets")),
        "links": _clean_str_list(d.get("links")),
    }


def _clean_section_input(section: Any) -> tuple[str, str | None, list[str], list[Any]] | None:
    """Coerce a model-supplied section object into ``(name, text, items, raw_entries)``,
    or ``None`` if ``section`` isn't an object. ``raw_entries`` is NOT yet cleaned —
    the caller mints an id per entry as it cleans each one."""
    if not isinstance(section, dict):
        return None
    d = {k: v for k, v in section.items() if k != "id"}
    name = _clean_str_or_none(d.get("name")) or ""
    text = _clean_str_or_none(d.get("text"))
    items = _clean_str_list(d.get("items"))
    entries = d.get("entries")
    entries = entries if isinstance(entries, list) else []
    return name, text, items, entries


# --- CV working copy -------------------------------------------------------------------


class CvWorkingCopy:
    """A mutable projection of a ``CVDocument``, addressable by minted section/entry ids.

    Section order is fixed as loaded (no reorder-sections tool exists in the
    vocabulary); only entries within a section can be added, removed, or reordered.
    """

    def __init__(self, cv: CVDocument) -> None:
        self._contact: dict[str, Any] = cv.contact.model_dump()
        self._section_order: list[str] = []
        self._sections: dict[str, dict[str, Any]] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._entry_section: dict[str, str] = {}
        self._retired: set[str] = set()
        self._next_section_seq = 1
        self._next_entry_seq = 1
        for section in cv.sections:
            sec_id = self._mint_section_id()
            entry_ids: list[str] = []
            for entry in section.entries:
                eid = self._mint_entry_id()
                self._entries[eid] = _entry_to_dict(entry)
                self._entry_section[eid] = sec_id
                entry_ids.append(eid)
            self._sections[sec_id] = {
                "name": section.name,
                "text": section.text,
                "items": list(section.items),
                "entry_ids": entry_ids,
            }
            self._section_order.append(sec_id)

    # -- id minting --------------------------------------------------------------

    def _mint_section_id(self) -> str:
        sec_id = f"s{self._next_section_seq}"
        self._next_section_seq += 1
        return sec_id

    def _mint_entry_id(self) -> str:
        eid = f"e{self._next_entry_seq}"
        self._next_entry_seq += 1
        return eid

    def _section_id_error(self, section_id: Any) -> dict[str, Any] | None:
        if not isinstance(section_id, str):
            return _err("bad_argument", "section_id must be a string")
        if section_id not in self._sections:
            return _id_error(section_id, self._retired, "call get_cv again")
        return None

    def _entry_id_error(self, entry_id: Any) -> dict[str, Any] | None:
        if not isinstance(entry_id, str):
            return _err("bad_argument", "entry_id must be a string")
        if entry_id not in self._entries:
            return _id_error(entry_id, self._retired, "call get_cv again")
        return None

    def _retire_entry(self, entry_id: str) -> None:
        section_id = self._entry_section.pop(entry_id)
        self._sections[section_id]["entry_ids"].remove(entry_id)
        del self._entries[entry_id]
        self._retired.add(entry_id)

    # -- read ----------------------------------------------------------------------

    def get_cv(self) -> dict[str, Any]:
        sections = []
        for sec_id in self._section_order:
            sec = self._sections[sec_id]
            entries = [{"id": eid, **self._entries[eid]} for eid in sec["entry_ids"]]
            sections.append({
                "id": sec_id,
                "name": sec["name"],
                "text": sec["text"],
                "items": list(sec["items"]),
                "entries": entries,
            })
        return _ok(contact=dict(self._contact), sections=sections)

    # -- mutations -------------------------------------------------------------------

    def replace_summary(self, text: Any) -> dict[str, Any]:
        cleaned = _clean_str_or_none(text)
        if cleaned is None:
            return _err("bad_argument", "text must be a non-empty string")
        for sec_id in self._section_order:
            if SUMMARY_NAME_RE.search(self._sections[sec_id]["name"] or ""):
                self._sections[sec_id]["text"] = cleaned
                return _ok(id=sec_id)
        sec_id = self._mint_section_id()
        self._sections[sec_id] = {"name": "Summary", "text": cleaned, "items": [], "entry_ids": []}
        self._section_order.insert(0, sec_id)
        return _ok(id=sec_id)

    def replace_section(self, section_id: Any, section: Any) -> dict[str, Any]:
        err = self._section_id_error(section_id)
        if err is not None:
            return err
        clean = _clean_section_input(section)
        if clean is None:
            return _err("bad_argument", "section must be an object")
        name, text, items, raw_entries = clean
        if not name:
            return _err("bad_argument", "section.name must be a non-empty string")
        for eid in list(self._sections[section_id]["entry_ids"]):
            self._retire_entry(eid)
        new_entry_ids: list[str] = []
        for raw_entry in raw_entries:
            cleaned_entry = _clean_entry_input(raw_entry)
            if cleaned_entry is None:
                continue  # non-object entries are dropped, not fatal (tolerant absorption)
            eid = self._mint_entry_id()
            self._entries[eid] = cleaned_entry
            self._entry_section[eid] = section_id
            new_entry_ids.append(eid)
        self._sections[section_id] = {
            "name": name, "text": text, "items": items, "entry_ids": new_entry_ids,
        }
        return _ok(id=section_id)

    def replace_entry(self, entry_id: Any, entry: Any) -> dict[str, Any]:
        err = self._entry_id_error(entry_id)
        if err is not None:
            return err
        cleaned = _clean_entry_input(entry)
        if cleaned is None:
            return _err("bad_argument", "entry must be an object")
        self._entries[entry_id] = cleaned
        return _ok(id=entry_id)

    def edit_entry_bullets(self, entry_id: Any, bullets: Any) -> dict[str, Any]:
        err = self._entry_id_error(entry_id)
        if err is not None:
            return err
        if not isinstance(bullets, list) or not all(isinstance(b, str) for b in bullets):
            return _err("bad_argument", "bullets must be a list of strings")
        self._entries[entry_id]["bullets"] = _clean_str_list(bullets)
        return _ok(id=entry_id)

    def add_entry(self, section_id: Any, entry: Any, position: Any = None) -> dict[str, Any]:
        err = self._section_id_error(section_id)
        if err is not None:
            return err
        cleaned = _clean_entry_input(entry)
        if cleaned is None:
            return _err("bad_argument", "entry must be an object")
        if position is not None and not isinstance(position, int):
            return _err("bad_argument", "position must be an integer or null")
        eid = self._mint_entry_id()
        self._entries[eid] = cleaned
        self._entry_section[eid] = section_id
        order = self._sections[section_id]["entry_ids"]
        idx = len(order) if position is None else max(0, min(position, len(order)))
        order.insert(idx, eid)
        return _ok(id=eid)

    def remove_entry(self, entry_id: Any) -> dict[str, Any]:
        err = self._entry_id_error(entry_id)
        if err is not None:
            return err
        self._retire_entry(entry_id)
        return _ok(id=entry_id)

    def reorder_entries(self, section_id: Any, order: Any) -> dict[str, Any]:
        err = self._section_id_error(section_id)
        if err is not None:
            return err
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            return _err("bad_argument", "order must be a list of strings")
        current = self._sections[section_id]["entry_ids"]
        if sorted(order) != sorted(current):
            return _err(
                "bad_argument",
                "order must contain exactly the section's current entry ids, each once",
            )
        self._sections[section_id]["entry_ids"] = list(order)
        return _ok(id=section_id)

    # -- finalize --------------------------------------------------------------------

    def _to_document_dict(self) -> dict[str, Any]:
        sections = []
        for sec_id in self._section_order:
            sec = self._sections[sec_id]
            sections.append({
                "name": sec["name"],
                "text": sec["text"],
                "items": list(sec["items"]),
                "entries": [dict(self._entries[eid]) for eid in sec["entry_ids"]],
            })
        return {"contact": dict(self._contact), "sections": sections}

    def finalize(
        self,
        *,
        language: str = "en",
        structured: bool = False,
        reemit_hint: str | None = None,
    ) -> dict[str, Any]:
        """Validate the working copy and return the canonical ``CVDocument``, or a
        ``validation_failed`` error result.

        ``structured`` selects ``_validate_final_content``'s re-emit wording on
        failure — it must match whatever mode the calling session is actually in
        (see ``jsa/pipeline/validation.py``); ``reemit_hint``, when given, overrides
        it entirely. ``jsa/pipeline/tool_loop.py`` (Phase 2) always passes its own
        tool-specific fix-and-retry instruction here — neither of the two built-in
        sentinel-vs-structured wordings fits a tool-patching turn, since the failure
        comes back as an ordinary ``finalize`` tool result, not a whole re-emitted
        document.
        """
        payload = json.dumps(self._to_document_dict())
        try:
            document = _validate_final_content(
                Stage.cv_adjust, payload, None, language,
                structured=structured, reemit_hint=reemit_hint,
            )
        except FinalContentError as exc:
            return _err("validation_failed", str(exc))
        return _ok(document=document)


# --- cover-letter working copy ----------------------------------------------------------


class ClWorkingCopy:
    """A mutable projection of a ``CoverLetter``, addressable by minted paragraph ids."""

    def __init__(self, cl: CoverLetter) -> None:
        self._salutation: str | None = cl.salutation
        self._signoff: str | None = cl.signoff
        self._paragraphs: dict[str, str] = {}
        self._paragraph_order: list[str] = []
        self._retired: set[str] = set()
        self._next_paragraph_seq = 1
        for text in cl.paragraphs:
            pid = self._mint_paragraph_id()
            self._paragraphs[pid] = text
            self._paragraph_order.append(pid)

    def _mint_paragraph_id(self) -> str:
        pid = f"p{self._next_paragraph_seq}"
        self._next_paragraph_seq += 1
        return pid

    def _paragraph_id_error(self, paragraph_id: Any) -> dict[str, Any] | None:
        if not isinstance(paragraph_id, str):
            return _err("bad_argument", "paragraph_id must be a string")
        if paragraph_id not in self._paragraphs:
            return _id_error(paragraph_id, self._retired, "call get_letter again")
        return None

    # -- read ----------------------------------------------------------------------

    def get_letter(self) -> dict[str, Any]:
        return _ok(
            salutation=self._salutation,
            signoff=self._signoff,
            paragraphs=[
                {"id": pid, "text": self._paragraphs[pid]} for pid in self._paragraph_order
            ],
        )

    # -- mutations -------------------------------------------------------------------

    def replace_paragraph(self, paragraph_id: Any, text: Any) -> dict[str, Any]:
        err = self._paragraph_id_error(paragraph_id)
        if err is not None:
            return err
        cleaned = _clean_str_or_none(text)
        if cleaned is None:
            return _err("bad_argument", "text must be a non-empty string")
        self._paragraphs[paragraph_id] = cleaned
        return _ok(id=paragraph_id)

    def add_paragraph(self, text: Any, position: Any = None) -> dict[str, Any]:
        cleaned = _clean_str_or_none(text)
        if cleaned is None:
            return _err("bad_argument", "text must be a non-empty string")
        if position is not None and not isinstance(position, int):
            return _err("bad_argument", "position must be an integer or null")
        pid = self._mint_paragraph_id()
        self._paragraphs[pid] = cleaned
        idx = (
            len(self._paragraph_order)
            if position is None
            else max(0, min(position, len(self._paragraph_order)))
        )
        self._paragraph_order.insert(idx, pid)
        return _ok(id=pid)

    def remove_paragraph(self, paragraph_id: Any) -> dict[str, Any]:
        err = self._paragraph_id_error(paragraph_id)
        if err is not None:
            return err
        self._paragraph_order.remove(paragraph_id)
        del self._paragraphs[paragraph_id]
        self._retired.add(paragraph_id)
        return _ok(id=paragraph_id)

    def reorder_paragraphs(self, order: Any) -> dict[str, Any]:
        if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
            return _err("bad_argument", "order must be a list of strings")
        if sorted(order) != sorted(self._paragraph_order):
            return _err(
                "bad_argument",
                "order must contain exactly the letter's current paragraph ids, each once",
            )
        self._paragraph_order = list(order)
        return _ok()

    def set_salutation(self, text: Any) -> dict[str, Any]:
        if text is not None and not isinstance(text, str):
            return _err("bad_argument", "text must be a string or null")
        self._salutation = _clean_str_or_none(text)
        return _ok()

    def set_signoff(self, text: Any) -> dict[str, Any]:
        if text is not None and not isinstance(text, str):
            return _err("bad_argument", "text must be a string or null")
        self._signoff = _clean_str_or_none(text)
        return _ok()

    # -- finalize --------------------------------------------------------------------

    def _to_document_dict(self) -> dict[str, Any]:
        return {
            "salutation": self._salutation,
            "signoff": self._signoff,
            "paragraphs": [self._paragraphs[pid] for pid in self._paragraph_order],
        }

    def finalize(
        self,
        *,
        language: str = "en",
        structured: bool = False,
        reemit_hint: str | None = None,
    ) -> dict[str, Any]:
        """See ``CvWorkingCopy.finalize``'s docstring for the ``structured``/
        ``reemit_hint`` contract."""
        payload = json.dumps(self._to_document_dict())
        try:
            document = _validate_final_content(
                Stage.cover_letter, payload, None, language,
                structured=structured, reemit_hint=reemit_hint,
            )
        except FinalContentError as exc:
            return _err("validation_failed", str(exc))
        return _ok(document=document)


__all__ = ["CvWorkingCopy", "ClWorkingCopy"]
