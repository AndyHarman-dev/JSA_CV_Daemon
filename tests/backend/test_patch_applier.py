"""jsa/schema/patch.py — CvWorkingCopy / ClWorkingCopy applier.

Revision-tool-use plan, Phase 1. Every tool: happy path, unknown id, retired-after-
removal id, malformed arguments. Plus the id-stability property, finalize's
idempotence-on-an-unmodified-copy guarantee, and finalize rejecting an invalid result
(too-short letter, CV with no renderable content) via the same gate as a full rewrite.
"""

from __future__ import annotations

import json

import pytest

from jsa.render.serialize import cover_letter_to_markdown, cv_to_markdown
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument
from jsa.schema.patch import ClWorkingCopy, CvWorkingCopy

# --- fixtures ----------------------------------------------------------------------------

_CV_JSON = {
    "contact": {"name": "Jane Doe", "email": "jane@example.com"},
    "sections": [
        {"name": "Summary", "text": "Experienced engineer."},
        {
            "name": "Experience",
            "entries": [
                {"heading": "Engineer", "subheading": "Acme", "bullets": ["Did X", "Did Y"]},
                {"heading": "Intern", "subheading": "Widgets Inc", "bullets": ["Did Z"]},
            ],
        },
    ],
}

# Exercises every field the plain CVDocument schema supports — text, items, entries,
# bullets, dates, location and links — per the plan's idempotence-prerequisite note.
_RICH_CV_JSON = {
    "contact": {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": "555-1234",
        "location": "New York, NY",
        "links": ["https://linkedin.com/in/janedoe"],
    },
    "sections": [
        {"name": "Summary", "text": "Experienced engineer."},
        {"name": "Skills", "items": ["Python", "SQL", "Docker"]},
        {
            "name": "Experience",
            "entries": [
                {
                    "heading": "Engineer",
                    "subheading": "Acme",
                    "dates": "2020-2023",
                    "location": "Remote",
                    "text": "Led a small team.",
                    "bullets": ["Did X", "Did Y"],
                    "links": ["https://github.com/janedoe/project"],
                },
                {
                    "heading": "Intern",
                    "subheading": "Widgets Inc",
                    "dates": "2019",
                    "bullets": ["Did Z"],
                },
            ],
        },
    ],
}

# Two paragraphs, each comfortably past MIN_COVER_LETTER_CHARS combined.
_CL_JSON = {
    "salutation": "Dear Hiring Manager,",
    "paragraphs": [
        "This is a decently long opening paragraph that clears the minimum character "
        "threshold easily by quite a margin indeed, on its own.",
        "A second paragraph as well, also long enough to matter for the total length "
        "check performed at validation time by the schema.",
    ],
    "signoff": "Sincerely,\nJane Doe",
}


def _cv() -> CVDocument:
    return CVDocument.model_validate(_CV_JSON)


def _cl() -> CoverLetter:
    return CoverLetter.model_validate(_CL_JSON)


# --- CvWorkingCopy: get_cv / ids -----------------------------------------------------------


class TestGetCv:
    def test_ok_and_shape(self):
        wc = CvWorkingCopy(_cv())
        result = wc.get_cv()
        assert result["ok"] is True
        assert result["contact"]["name"] == "Jane Doe"
        assert len(result["sections"]) == 2
        for section in result["sections"]:
            assert "id" in section
            for entry in section["entries"]:
                assert "id" in entry

    def test_ids_are_stable_across_repeated_get_cv_calls(self):
        wc = CvWorkingCopy(_cv())
        first = wc.get_cv()
        second = wc.get_cv()
        assert [s["id"] for s in first["sections"]] == [s["id"] for s in second["sections"]]


# --- replace_summary -----------------------------------------------------------------------


class TestReplaceSummary:
    def test_updates_existing_summary(self):
        wc = CvWorkingCopy(_cv())
        result = wc.replace_summary("New summary text.")
        assert result["ok"] is True
        cv = wc.get_cv()
        assert cv["sections"][0]["name"] == "Summary"
        assert cv["sections"][0]["text"] == "New summary text."

    def test_inserts_summary_when_absent(self):
        cv_no_summary = CVDocument.model_validate({
            "contact": {"name": "X"},
            "sections": [{"name": "Skills", "items": ["Python"]}],
        })
        wc = CvWorkingCopy(cv_no_summary)
        result = wc.replace_summary("A fresh summary.")
        assert result["ok"] is True
        cv = wc.get_cv()
        assert cv["sections"][0]["name"] == "Summary"
        assert cv["sections"][0]["text"] == "A fresh summary."
        assert cv["sections"][1]["name"] == "Skills"

    def test_bad_argument_empty_text(self):
        wc = CvWorkingCopy(_cv())
        result = wc.replace_summary("   ")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_non_string(self):
        wc = CvWorkingCopy(_cv())
        result = wc.replace_summary(123)
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


# --- replace_section -------------------------------------------------------------------------


class TestReplaceSection:
    def test_happy_path(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][0]["id"]
        result = wc.replace_section(sec_id, {"name": "Profile", "text": "Rewritten."})
        assert result == {"ok": True, "id": sec_id}
        cv = wc.get_cv()
        assert cv["sections"][0]["name"] == "Profile"
        assert cv["sections"][0]["text"] == "Rewritten."

    def test_replacing_entries_retires_old_entry_ids(self):
        wc = CvWorkingCopy(_cv())
        exp = wc.get_cv()["sections"][1]
        old_entry_id = exp["entries"][0]["id"]
        result = wc.replace_section(
            exp["id"],
            {"name": "Experience", "entries": [{"heading": "New Role", "bullets": ["A"]}]},
        )
        assert result["ok"] is True
        # the old entry id is now stale, not unknown
        stale = wc.edit_entry_bullets(old_entry_id, ["x"])
        assert stale == {
            "ok": False,
            "error": {
                "code": "stale_id",
                "message": f"id {old_entry_id!r} no longer exists (it was removed)",
                "hint": "call get_cv again",
            },
        }

    def test_strips_model_supplied_id_defense_in_depth(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][0]["id"]
        wc.replace_section(sec_id, {"id": "spoofed", "name": "Profile", "text": "Rewritten."})
        cv = wc.get_cv()
        assert cv["sections"][0]["id"] == sec_id
        assert cv["sections"][0]["id"] != "spoofed"

    def test_unknown_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.replace_section("s999", {"name": "X"})
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_retired_id_after_removal(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][0]["id"]
        wc.replace_section(sec_id, {"name": "Profile"})  # still a valid id, just repurposed
        # Simulate retirement via a full CV round-trip is out of scope for sections (no
        # remove-section tool exists in the vocabulary) — covered instead at entry level
        # below (TestRemoveEntry / ID stability).
        assert wc.get_cv()["sections"][0]["id"] == sec_id

    def test_bad_argument_not_an_object(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][0]["id"]
        result = wc.replace_section(sec_id, "not an object")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_missing_name(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][0]["id"]
        result = wc.replace_section(sec_id, {"text": "no name given"})
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_non_object_entries_are_dropped_not_fatal(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        result = wc.replace_section(
            sec_id, {"name": "Experience", "entries": ["not an object", {"heading": "Real"}]}
        )
        assert result["ok"] is True
        cv = wc.get_cv()
        exp = next(s for s in cv["sections"] if s["id"] == sec_id)
        assert len(exp["entries"]) == 1
        assert exp["entries"][0]["heading"] == "Real"


# --- replace_entry ---------------------------------------------------------------------------


class TestReplaceEntry:
    def test_happy_path(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.replace_entry(
            entry_id, {"heading": "Senior Engineer", "bullets": ["New bullet"]}
        )
        assert result == {"ok": True, "id": entry_id}
        cv = wc.get_cv()
        entry = cv["sections"][1]["entries"][0]
        assert entry["id"] == entry_id  # stays in place
        assert entry["heading"] == "Senior Engineer"
        assert entry["bullets"] == ["New bullet"]
        assert entry["subheading"] is None  # not carried over — full replace

    def test_unknown_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.replace_entry("e999", {"heading": "X"})
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_retired_id_after_removal(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        wc.remove_entry(entry_id)
        result = wc.replace_entry(entry_id, {"heading": "X"})
        assert result["ok"] is False
        assert result["error"]["code"] == "stale_id"

    def test_bad_argument_not_an_object(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.replace_entry(entry_id, ["not", "an", "object"])
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_strips_model_supplied_id(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        wc.replace_entry(entry_id, {"id": "spoofed", "heading": "X"})
        cv = wc.get_cv()
        ids = [e["id"] for e in cv["sections"][1]["entries"]]
        assert "spoofed" not in ids
        assert entry_id in ids


# --- edit_entry_bullets ---------------------------------------------------------------------


class TestEditEntryBullets:
    def test_happy_path(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.edit_entry_bullets(entry_id, ["Only bullet now"])
        assert result == {"ok": True, "id": entry_id}
        cv = wc.get_cv()
        assert cv["sections"][1]["entries"][0]["bullets"] == ["Only bullet now"]
        # other fields untouched
        assert cv["sections"][1]["entries"][0]["heading"] == "Engineer"

    def test_unknown_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.edit_entry_bullets("e999", ["x"])
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_retired_id_after_removal(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        wc.remove_entry(entry_id)
        result = wc.edit_entry_bullets(entry_id, ["x"])
        assert result["ok"] is False
        assert result["error"]["code"] == "stale_id"

    def test_bad_argument_not_a_list(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.edit_entry_bullets(entry_id, "not a list")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_list_with_non_strings(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.edit_entry_bullets(entry_id, ["ok", 123])
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


# --- add_entry / remove_entry / reorder_entries ----------------------------------------------


class TestAddEntry:
    def test_happy_path_append(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        result = wc.add_entry(sec_id, {"heading": "New Role", "bullets": ["A"]})
        assert result["ok"] is True
        new_id = result["id"]
        cv = wc.get_cv()
        entries = cv["sections"][1]["entries"]
        assert entries[-1]["id"] == new_id
        assert entries[-1]["heading"] == "New Role"

    def test_happy_path_with_position(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        result = wc.add_entry(sec_id, {"heading": "First"}, position=0)
        assert result["ok"] is True
        cv = wc.get_cv()
        assert cv["sections"][1]["entries"][0]["heading"] == "First"

    def test_unknown_section_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.add_entry("s999", {"heading": "X"})
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_bad_argument_entry_not_object(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        result = wc.add_entry(sec_id, "nope")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_position_not_int(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        result = wc.add_entry(sec_id, {"heading": "X"}, position="first")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


class TestRemoveEntry:
    def test_happy_path(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        result = wc.remove_entry(entry_id)
        assert result == {"ok": True, "id": entry_id}
        cv = wc.get_cv()
        ids = [e["id"] for e in cv["sections"][1]["entries"]]
        assert entry_id not in ids

    def test_unknown_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.remove_entry("e999")
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_removing_twice_is_stale_not_unknown(self):
        wc = CvWorkingCopy(_cv())
        entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        wc.remove_entry(entry_id)
        result = wc.remove_entry(entry_id)
        assert result["ok"] is False
        assert result["error"]["code"] == "stale_id"


class TestIdStability:
    """The load-bearing property: an id read before an unrelated mutation still
    resolves correctly afterward — a multi-edit revision fits in one get_cv read."""

    def test_edit_by_id_read_before_an_unrelated_removal_still_hits_the_right_entry(self):
        wc = CvWorkingCopy(_cv())
        cv = wc.get_cv()
        first_entry_id = cv["sections"][1]["entries"][0]["id"]
        second_entry_id = cv["sections"][1]["entries"][1]["id"]

        wc.remove_entry(second_entry_id)
        result = wc.edit_entry_bullets(first_entry_id, ["Still works"])

        assert result["ok"] is True
        after = wc.get_cv()
        assert after["sections"][1]["entries"][0]["id"] == first_entry_id
        assert after["sections"][1]["entries"][0]["bullets"] == ["Still works"]

    def test_added_entry_id_never_collides_with_a_retired_one(self):
        wc = CvWorkingCopy(_cv())
        sec_id = wc.get_cv()["sections"][1]["id"]
        first_entry_id = wc.get_cv()["sections"][1]["entries"][0]["id"]
        wc.remove_entry(first_entry_id)
        added = wc.add_entry(sec_id, {"heading": "Fresh"})
        assert added["id"] != first_entry_id


class TestReorderEntries:
    def test_happy_path(self):
        wc = CvWorkingCopy(_cv())
        sec = wc.get_cv()["sections"][1]
        ids = [e["id"] for e in sec["entries"]]
        result = wc.reorder_entries(sec["id"], list(reversed(ids)))
        assert result["ok"] is True
        after = wc.get_cv()["sections"][1]["entries"]
        assert [e["id"] for e in after] == list(reversed(ids))

    def test_unknown_section_id(self):
        wc = CvWorkingCopy(_cv())
        result = wc.reorder_entries("s999", [])
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_bad_argument_not_a_permutation(self):
        wc = CvWorkingCopy(_cv())
        sec = wc.get_cv()["sections"][1]
        result = wc.reorder_entries(sec["id"], ["bogus-id"])
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_missing_a_member(self):
        wc = CvWorkingCopy(_cv())
        sec = wc.get_cv()["sections"][1]
        ids = [e["id"] for e in sec["entries"]]
        result = wc.reorder_entries(sec["id"], ids[:1])  # drops the second id
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_bad_argument_not_a_list(self):
        wc = CvWorkingCopy(_cv())
        sec = wc.get_cv()["sections"][1]
        result = wc.reorder_entries(sec["id"], "not a list")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


# --- finalize (CV) --------------------------------------------------------------------------


class TestCvFinalize:
    def test_unmodified_copy_reproduces_input_byte_for_byte_via_markdown(self):
        cv = _cv()
        wc = CvWorkingCopy(cv)
        result = wc.finalize()
        assert result["ok"] is True
        assert cv_to_markdown(result["document"]) == cv_to_markdown(cv)

    def test_unmodified_rich_copy_reproduces_input_byte_for_byte_via_markdown(self):
        # Same guarantee, but on a document that exercises every field (items, dates,
        # location, links) the simpler _CV_JSON fixture above never touches.
        cv = CVDocument.model_validate(_RICH_CV_JSON)
        wc = CvWorkingCopy(cv)
        result = wc.finalize()
        assert result["ok"] is True
        assert cv_to_markdown(result["document"]) == cv_to_markdown(cv)

    def test_rejects_a_cv_emptied_of_all_renderable_content(self):
        cv = CVDocument.model_validate({
            "contact": {"name": "X"},
            "sections": [{"name": "Experience", "entries": [{"heading": "Job"}]}],
        })
        wc = CvWorkingCopy(cv)
        entry_id = wc.get_cv()["sections"][0]["entries"][0]["id"]
        wc.remove_entry(entry_id)
        result = wc.finalize()
        assert result["ok"] is False
        assert result["error"]["code"] == "validation_failed"

    def test_finalize_after_valid_edits_reflects_them(self):
        wc = CvWorkingCopy(_cv())
        wc.replace_summary("Edited summary.")
        result = wc.finalize()
        assert result["ok"] is True
        assert "Edited summary." in cv_to_markdown(result["document"])

    def _emptied_cv_working_copy(self) -> CvWorkingCopy:
        cv = CVDocument.model_validate({
            "contact": {"name": "X"},
            "sections": [{"name": "Experience", "entries": [{"heading": "Job"}]}],
        })
        wc = CvWorkingCopy(cv)
        entry_id = wc.get_cv()["sections"][0]["entries"][0]["id"]
        wc.remove_entry(entry_id)
        return wc

    def test_default_structured_flag_uses_sentinel_wording(self):
        result = self._emptied_cv_working_copy().finalize()
        assert "<<<FINAL>>>" in result["error"]["message"]
        assert "structured reply" not in result["error"]["message"]

    def test_structured_true_uses_structured_reply_wording(self):
        result = self._emptied_cv_working_copy().finalize(structured=True)
        assert "structured reply's `payload`" in result["error"]["message"]
        assert "<<<FINAL>>>" not in result["error"]["message"]


# --- ClWorkingCopy ---------------------------------------------------------------------------


class TestGetLetter:
    def test_ok_and_shape(self):
        wcl = ClWorkingCopy(_cl())
        result = wcl.get_letter()
        assert result["ok"] is True
        assert result["salutation"] == "Dear Hiring Manager,"
        assert result["signoff"] == "Sincerely,\nJane Doe"
        assert len(result["paragraphs"]) == 2
        for p in result["paragraphs"]:
            assert "id" in p


class TestReplaceParagraph:
    def test_happy_path(self):
        wcl = ClWorkingCopy(_cl())
        pid = wcl.get_letter()["paragraphs"][0]["id"]
        result = wcl.replace_paragraph(pid, "A brand new opening line, long enough on its own.")
        assert result == {"ok": True, "id": pid}
        assert wcl.get_letter()["paragraphs"][0]["text"].startswith("A brand new opening")

    def test_unknown_id(self):
        wcl = ClWorkingCopy(_cl())
        result = wcl.replace_paragraph("p999", "text")
        assert result["ok"] is False
        assert result["error"]["code"] == "unknown_id"

    def test_retired_id_after_removal(self):
        wcl = ClWorkingCopy(_cl())
        pid = wcl.get_letter()["paragraphs"][0]["id"]
        wcl.remove_paragraph(pid)
        result = wcl.replace_paragraph(pid, "text")
        assert result["ok"] is False
        assert result["error"]["code"] == "stale_id"

    def test_bad_argument_empty_text(self):
        wcl = ClWorkingCopy(_cl())
        pid = wcl.get_letter()["paragraphs"][0]["id"]
        result = wcl.replace_paragraph(pid, "   ")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


class TestAddRemoveReorderParagraphs:
    def test_add_paragraph_append_and_position(self):
        wcl = ClWorkingCopy(_cl())
        added = wcl.add_paragraph("A tail paragraph.")
        assert added["ok"] is True
        letter = wcl.get_letter()
        assert letter["paragraphs"][-1]["id"] == added["id"]

        added_first = wcl.add_paragraph("A head paragraph.", position=0)
        letter = wcl.get_letter()
        assert letter["paragraphs"][0]["id"] == added_first["id"]

    def test_add_paragraph_bad_argument(self):
        wcl = ClWorkingCopy(_cl())
        result = wcl.add_paragraph("")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_remove_paragraph_happy_and_unknown(self):
        wcl = ClWorkingCopy(_cl())
        pid = wcl.get_letter()["paragraphs"][0]["id"]
        assert wcl.remove_paragraph(pid) == {"ok": True, "id": pid}
        assert wcl.remove_paragraph(pid)["error"]["code"] == "stale_id"
        assert wcl.remove_paragraph("p999")["error"]["code"] == "unknown_id"

    def test_reorder_happy_path(self):
        wcl = ClWorkingCopy(_cl())
        ids = [p["id"] for p in wcl.get_letter()["paragraphs"]]
        result = wcl.reorder_paragraphs(list(reversed(ids)))
        assert result["ok"] is True
        after = [p["id"] for p in wcl.get_letter()["paragraphs"]]
        assert after == list(reversed(ids))

    def test_reorder_bad_argument(self):
        wcl = ClWorkingCopy(_cl())
        result = wcl.reorder_paragraphs(["bogus"])
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


class TestSetSalutationSignoff:
    def test_set_and_clear_salutation(self):
        wcl = ClWorkingCopy(_cl())
        assert wcl.set_salutation("Dear Team,") == {"ok": True}
        assert wcl.get_letter()["salutation"] == "Dear Team,"
        assert wcl.set_salutation(None) == {"ok": True}
        assert wcl.get_letter()["salutation"] is None

    def test_set_and_clear_signoff(self):
        wcl = ClWorkingCopy(_cl())
        assert wcl.set_signoff("Best,\nJ.") == {"ok": True}
        assert wcl.get_letter()["signoff"] == "Best,\nJ."
        assert wcl.set_signoff(None) == {"ok": True}
        assert wcl.get_letter()["signoff"] is None

    def test_bad_argument_non_string_non_null(self):
        wcl = ClWorkingCopy(_cl())
        result = wcl.set_salutation(123)
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"


# --- finalize (CL) --------------------------------------------------------------------------


class TestClFinalize:
    def test_unmodified_copy_reproduces_input_byte_for_byte_via_markdown(self):
        cl = _cl()
        wcl = ClWorkingCopy(cl)
        result = wcl.finalize()
        assert result["ok"] is True
        assert cover_letter_to_markdown(result["document"]) == cover_letter_to_markdown(cl)

    def test_rejects_a_letter_pushed_under_the_minimum_length(self):
        wcl = ClWorkingCopy(_cl())
        for p in wcl.get_letter()["paragraphs"]:
            wcl.remove_paragraph(p["id"])
        wcl.add_paragraph("Too short.")
        result = wcl.finalize()
        assert result["ok"] is False
        assert result["error"]["code"] == "validation_failed"

    def test_finalize_after_valid_edits_reflects_them(self):
        wcl = ClWorkingCopy(_cl())
        wcl.set_signoff("Warmly,\nJ.")
        result = wcl.finalize()
        assert result["ok"] is True
        assert "Warmly," in cover_letter_to_markdown(result["document"])


# --- idempotence prerequisite ----------------------------------------------------------
#
# The applier's core assumption is that Section._classify / Entry._normalize (and
# CoverLetter's own normalizer) are FIXED POINTS on already-canonical input — patch.py
# feeds them plain dicts shaped exactly like a prior validated document's own fields
# (see _to_document_dict on both working copies), never re-deriving field placement.
# This pins that assumption directly against the schemas themselves, independent of
# patch.py, on a document exercising every field the plain schema supports (text,
# items, entries, bullets, dates, location, links) — per the plan's Phase 1 note.


class TestIdempotencePrerequisite:
    def test_cv_round_trip_is_a_fixed_point(self):
        cv = CVDocument.model_validate(_RICH_CV_JSON)
        s = cv.model_dump_json()
        for _ in range(3):
            s2 = CVDocument.model_validate(json.loads(s)).model_dump_json()
            assert s2 == s
            s = s2

    def test_cover_letter_round_trip_is_a_fixed_point(self):
        cl = CoverLetter.model_validate(_CL_JSON)
        s = cl.model_dump_json()
        for _ in range(3):
            s2 = CoverLetter.model_validate(json.loads(s)).model_dump_json()
            assert s2 == s
            s = s2
