"""CV-editor AI chat plan, Phase 1: ``CvWorkingCopy.replace_contact``, ``CvChatOp``'s
per-op required-field validator, and ``json_schema_for_cv_chat``'s scope narrowing.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import ValidationError

from jsa.prompts import loader

from jsa.agents.tool_spec import CV_CHAT_OP_SPECS, CV_TOOL_SPECS, tools_for
from jsa.db.models import Stage
from jsa.pipeline.cv_chat import _apply_ops
from jsa.schema.chat_turn import CvChatOp, CvChatTurn, json_schema_for_cv_chat
from jsa.schema.cv import CVDocument
from jsa.schema.patch import CvWorkingCopy
from jsa.schema.turn_models import inline_defs

_CV_JSON = {
    "contact": {"name": "Jane Doe", "email": "jane@example.com", "phone": "555-1234"},
    "sections": [
        {"name": "Summary", "text": "Experienced engineer."},
        {
            "name": "Experience",
            "entries": [{"heading": "Engineer", "subheading": "Acme", "bullets": ["Did X"]}],
        },
    ],
}


def _copy() -> CvWorkingCopy:
    return CvWorkingCopy(CVDocument.model_validate(_CV_JSON))


class TestReplaceContact:
    def test_updates_only_named_fields(self):
        copy = _copy()
        result = copy.replace_contact({"email": "new@example.com"})
        assert result["ok"] is True
        assert result["fields"] == ["email"]
        dumped = copy.get_cv()["contact"]
        assert dumped["email"] == "new@example.com"
        assert dumped["name"] == "Jane Doe"
        assert dumped["phone"] == "555-1234"

    def test_coerces_links_to_a_string_list(self):
        copy = _copy()
        result = copy.replace_contact({"links": "https://example.com/jane"})
        assert result["ok"] is True
        assert copy.get_cv()["contact"]["links"] == ["https://example.com/jane"]

    def test_drops_unknown_keys(self):
        copy = _copy()
        result = copy.replace_contact({"email": "x@y.com", "bogus": "nope"})
        assert result["ok"] is True
        assert result["fields"] == ["email"]

    def test_non_object_is_bad_argument(self):
        copy = _copy()
        result = copy.replace_contact("not an object")
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_empty_object_is_bad_argument(self):
        copy = _copy()
        result = copy.replace_contact({})
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"

    def test_only_named_fields_survive_finalize(self):
        copy = _copy()
        copy.replace_contact({"location": "Berlin"})
        result = copy.finalize()
        assert result["ok"] is True
        assert result["document"].contact.location == "Berlin"
        assert result["document"].contact.name == "Jane Doe"

    def test_strips_whitespace_from_string_fields(self):
        copy = _copy()
        result = copy.replace_contact({"phone": "  555-9999  ", "location": "  Berlin  "})
        assert result["ok"] is True
        dumped = copy.get_cv()["contact"]
        assert dumped["phone"] == "555-9999"
        assert dumped["location"] == "Berlin"

    def test_blank_optional_field_clears_to_none(self):
        copy = _copy()
        result = copy.replace_contact({"phone": "   "})
        assert result["ok"] is True
        assert copy.get_cv()["contact"]["phone"] is None

    def test_blank_name_is_bad_argument_not_a_silent_clear(self):
        copy = _copy()
        result = copy.replace_contact({"name": "   "})
        assert result["ok"] is False
        assert result["error"]["code"] == "bad_argument"
        assert copy.get_cv()["contact"]["name"] == "Jane Doe"

    def test_name_is_stripped(self):
        copy = _copy()
        result = copy.replace_contact({"name": "  Jane Q. Doe  "})
        assert result["ok"] is True
        assert copy.get_cv()["contact"]["name"] == "Jane Q. Doe"


class TestChatOpSpecsIsolation:
    """D6: replace_contact must never widen the live job-revision vocabulary."""

    def test_cv_chat_op_specs_is_cv_tool_specs_plus_replace_contact(self):
        assert CV_CHAT_OP_SPECS[: len(CV_TOOL_SPECS)] == CV_TOOL_SPECS
        assert CV_CHAT_OP_SPECS[-1].name == "replace_contact"
        assert len(CV_CHAT_OP_SPECS) == len(CV_TOOL_SPECS) + 1

    def test_tools_for_revising_cv_excludes_replace_contact(self):
        names = {t.name for t in tools_for(Stage.revising_cv)}
        assert "replace_contact" not in names


def _valid_op(**overrides) -> dict:
    base = dict(
        op="replace_summary",
        section_id=None,
        entry_id=None,
        position=None,
        text="New summary",
        bullets=None,
        order=None,
        section=None,
        entry=None,
        contact=None,
    )
    base.update(overrides)
    return base


class TestCvChatOpValidation:
    def test_replace_summary_requires_text(self):
        with pytest.raises(ValidationError):
            CvChatOp.model_validate(_valid_op(text=None))

    def test_replace_summary_valid(self):
        op = CvChatOp.model_validate(_valid_op())
        assert op.op == "replace_summary"

    def test_replace_section_requires_section_id_and_section(self):
        with pytest.raises(ValidationError):
            CvChatOp.model_validate(
                _valid_op(op="replace_section", text=None, section_id="s1", section=None)
            )
        op = CvChatOp.model_validate(
            _valid_op(op="replace_section", text=None, section_id="s1", section={"name": "X"})
        )
        assert op.section_id == "s1"

    def test_add_entry_position_stays_optional(self):
        op = CvChatOp.model_validate(
            _valid_op(op="add_entry", text=None, section_id="s1", entry={"heading": "H"})
        )
        assert op.position is None

    def test_replace_contact_requires_contact(self):
        with pytest.raises(ValidationError):
            CvChatOp.model_validate(_valid_op(op="replace_contact", text=None, contact=None))
        op = CvChatOp.model_validate(
            _valid_op(op="replace_contact", text=None, contact={"email": "a@b.com"})
        )
        assert op.contact is not None
        assert op.contact.email == "a@b.com"
        # Omitted identity fields stay None so `_plain(..., exclude_none=True)` can
        # strip them -- see TestObjectArgumentSchemas below.
        assert op.contact.phone is None

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            CvChatOp.model_validate(_valid_op(bogus="nope"))


class TestObjectArgumentSchemas:
    """Regression: ``CvChatOp.section``/``entry``/``contact`` were ``dict[str, Any]``,
    which Pydantic renders as a bare ``{"type": "object"}`` -- no ``properties`` at
    all, and ``inline_defs`` strips the ``additionalProperties`` that was the only
    other key. A constrained decoder handed that has exactly one valid completion,
    ``{}``, so every ``replace_contact``/``replace_section``/``replace_entry`` came
    back empty while the model's ``answer`` claimed the edit was made (confirmed live
    against Gemini, 2026-09-16).

    Asserted on the INLINED schema specifically -- that is the form Gemini actually
    receives (``gemini_api.py`` is ``inline_defs``' only caller) and the form the bug
    was observable in.
    """

    @pytest.mark.parametrize("scope", ["cv", "contact", "section", "entry"])
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("contact", {"name", "email", "phone", "location", "links"}),
            ("entry", {"heading", "subheading", "dates", "location", "text", "bullets", "links"}),
            ("section", {"name", "text", "items", "entries"}),
        ],
    )
    def test_object_arguments_declare_their_properties(self, scope, field, expected):
        inlined = inline_defs(json_schema_for_cv_chat(scope))
        op = inlined["properties"]["payload"]["anyOf"][0]["properties"]["ops"]["items"]
        node = op["properties"][field]["anyOf"][0]
        assert set(node["properties"]) == expected
        # inline_defs' all-properties-required rewrite must reach these nested objects
        # too -- that is what stops the skeleton-emitting behaviour of commit c51592d.
        assert set(node["required"]) == expected

    def test_nested_entries_inside_section_are_also_fully_declared(self):
        inlined = inline_defs(json_schema_for_cv_chat("section"))
        op = inlined["properties"]["payload"]["anyOf"][0]["properties"]["ops"]["items"]
        entries = op["properties"]["section"]["anyOf"][0]["properties"]["entries"]
        assert set(entries["items"]["properties"]) == {
            "heading", "subheading", "dates", "location", "text", "bullets", "links",
        }

    def test_contact_links_is_nullable_so_not_changing_it_is_representable(self):
        """``inline_defs`` forces every property ``required``, so the model MUST emit
        ``links`` on a ``replace_contact`` turn. If the only value it could emit were
        ``[]``, ``exclude_none`` would not strip it and ``replace_contact`` would clear
        the user's links on every "fix the email" turn. ``null`` has to be available."""
        inlined = inline_defs(json_schema_for_cv_chat("contact"))
        op = inlined["properties"]["payload"]["anyOf"][0]["properties"]["ops"]["items"]
        links = op["properties"]["contact"]["anyOf"][0]["properties"]["links"]
        assert {"type": "null"} in links["anyOf"]


class TestContactPartialUpdateSurvivesTheTypedModel:
    """The typed ``ChatContact`` must not turn ``replace_contact``'s partial-update
    contract into a whole-object replace. Driven through ``_OP_DISPATCH`` rather than
    ``CvWorkingCopy`` directly, since the ``exclude_none=True`` that makes this work
    lives at the dispatch site."""

    def _apply(self, contact: dict) -> dict:
        copy = _copy()
        op = CvChatOp.model_validate(_valid_op(op="replace_contact", text=None, contact=contact))
        assert _apply_ops(copy, [op]) == []
        return copy.get_cv()["contact"]

    def test_explicit_nulls_do_not_blank_the_other_fields(self):
        # Exactly what a structured backend sends now that every property is required.
        result = self._apply(
            {"name": None, "email": "new@example.com", "phone": None, "location": None,
             "links": None}
        )
        assert result["email"] == "new@example.com"
        assert result["name"] == "Jane Doe"
        assert result["phone"] == "555-1234"

    def test_omitted_fields_do_not_blank_either(self):
        # What a sentinel-mode backend sends, following the prompt's "omit" wording.
        result = self._apply({"email": "new@example.com"})
        assert result["phone"] == "555-1234"
        assert result["name"] == "Jane Doe"

    def test_empty_links_list_still_clears_deliberately(self):
        copy = _copy()
        assert _apply_ops(copy, [CvChatOp.model_validate(
            _valid_op(op="replace_contact", text=None,
                      contact={"links": ["https://example.com/jane"]})
        )]) == []
        assert copy.get_cv()["contact"]["links"] == ["https://example.com/jane"]
        # `[]` is NOT stripped by exclude_none -- it is the only way to clear links.
        op = CvChatOp.model_validate(_valid_op(op="replace_contact", text=None,
                                               contact={"links": []}))
        assert _apply_ops(copy, [op]) == []
        assert copy.get_cv()["contact"]["links"] == []

    def test_an_empty_string_still_clears_a_scalar_field(self):
        """``exclude_none=True`` removes the "clear this" meaning that a bare ``null``
        used to carry, so "remove my phone number" needs a value that SURVIVES the dump.
        ``""`` does, and ``_clean_str_or_none("")`` is already ``None`` inside
        ``replace_contact``. Without this, that instruction would come back as a
        confident answer with an empty diff and no ``rejected`` entry -- the exact
        silent-success shape Finding A was about. ``PROMPT_CV_CHAT.md`` documents it."""
        result = self._apply({"phone": "", "email": None})
        assert result["phone"] is None
        assert result["email"] == "jane@example.com"

    def test_name_still_cannot_be_cleared(self):
        copy = _copy()
        op = CvChatOp.model_validate(_valid_op(op="replace_contact", text=None,
                                               contact={"name": ""}))
        rejected = _apply_ops(copy, [op])
        assert len(rejected) == 1
        assert copy.get_cv()["contact"]["name"] == "Jane Doe"

    def test_an_all_null_contact_is_rejected_per_op_not_applied_silently(self):
        copy = _copy()
        op = CvChatOp.model_validate(_valid_op(
            op="replace_contact", text=None,
            contact={"name": None, "email": None, "phone": None, "location": None,
                     "links": None},
        ))
        rejected = _apply_ops(copy, [op])
        assert len(rejected) == 1
        assert "replace_contact" in rejected[0]
        assert copy.get_cv()["contact"]["name"] == "Jane Doe"


class TestPromptExamplesStayValid:
    """``PROMPT_CV_CHAT.md``'s JSON shape examples are the ONLY channel that teaches
    these object shapes in sentinel mode -- ``chat_backend`` defaults to ``claude-cli``,
    which is handed no schema at all. Since ``ChatEntry``/``ChatSection``/``ChatContact``
    are ``extra="forbid"``, a typo'd or renamed field in an example would make every
    sentinel turn that faithfully copies it hard-fail the WHOLE turn, while this suite
    stayed green. Parsing the examples out of the prompt turns that into a drift guard
    -- the closest thing to sentinel-mode coverage available without a live CLI call."""

    @staticmethod
    def _examples() -> list[dict]:
        text = loader.read_prompt("cv_chat")
        blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        assert blocks, "no fenced json examples found in PROMPT_CV_CHAT.md"
        return [json.loads(b) for b in blocks]

    def test_every_object_example_validates_against_its_model(self):
        # Keyed by a field unique to each shape, so the test doesn't depend on the
        # examples' order or count in the prompt.
        seen = set()
        for example in self._examples():
            if "question" in example:
                continue  # the envelope illustration, not an op argument
            if "bullets" in example:
                CvChatOp.model_validate(_valid_op(
                    op="replace_entry", text=None, entry_id="e1", entry=example))
                seen.add("entry")
            elif "entries" in example:
                CvChatOp.model_validate(_valid_op(
                    op="replace_section", text=None, section_id="s1", section=example))
                seen.add("section")
            elif "email" in example:
                CvChatOp.model_validate(_valid_op(
                    op="replace_contact", text=None, contact=example))
                seen.add("contact")
            else:
                pytest.fail(f"unrecognized json example in the prompt: {example!r}")
        assert seen == {"entry", "section", "contact"}, f"missing examples for {seen}"


class TestJsonSchemaForCvChat:
    def test_narrows_op_enum_per_scope(self):
        contact_schema = json_schema_for_cv_chat("contact")
        entry_schema = json_schema_for_cv_chat("entry")
        cv_schema = json_schema_for_cv_chat("cv")
        assert contact_schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"] == [
            "replace_contact"
        ]
        assert set(entry_schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"]) == {
            "replace_entry",
            "edit_entry_bullets",
        }
        assert len(cv_schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"]) == 8

    def test_deterministic_across_repeated_calls(self):
        a = json.dumps(json_schema_for_cv_chat("section"), sort_keys=True)
        b = json.dumps(json_schema_for_cv_chat("section"), sort_keys=True)
        assert a == b


class TestShapeNarrowing:
    """`has_entries=False` drops the ops that can only address an entry that exists.

    Live failure this prevents (2026-09-18): asked to compact the entry-less `Summary`
    section, `qwen/qwen3-30b-a3b-instruct-2507` answered `edit_entry_bullets` with
    `entry_id: null` — a cross-field ValidationError, and `run_cv_chat` has no
    self-heal budget, so the whole turn was lost with an HTTP 422.
    """

    ENTRY_ADDRESSING = {"replace_entry", "edit_entry_bullets", "remove_entry", "reorder_entries"}

    def _enum(self, scope, **kw):
        return json_schema_for_cv_chat(scope, **kw)["$defs"]["CvChatOp"]["properties"]["op"]["enum"]

    def test_omitting_has_entries_is_byte_identical_to_pre_narrowing(self):
        """The narrowing is opt-in: every pre-existing caller must be unaffected."""
        for scope in ("cv", "contact", "section", "entry"):
            assert json.dumps(json_schema_for_cv_chat(scope), sort_keys=True) == json.dumps(
                json_schema_for_cv_chat(scope, has_entries=None), sort_keys=True
            )

    def test_section_without_entries_drops_entry_addressing_ops(self):
        narrowed = self._enum("section", has_entries=False)
        assert self.ENTRY_ADDRESSING.isdisjoint(narrowed)
        assert set(narrowed) == {"replace_section", "replace_summary", "add_entry"}

    def test_add_entry_survives_because_it_addresses_the_section(self):
        """`add_entry` needs only `section_id`, so it stays satisfiable on an empty
        section — dropping it would remove a real capability, not an impossible one."""
        assert "add_entry" in self._enum("section", has_entries=False)
        assert "add_entry" in self._enum("cv", has_entries=False)

    def test_cv_scope_narrows_too(self):
        narrowed = self._enum("cv", has_entries=False)
        assert self.ENTRY_ADDRESSING.isdisjoint(narrowed)
        assert {"replace_summary", "replace_section", "replace_contact"} <= set(narrowed)

    def test_has_entries_true_is_the_full_vocabulary(self):
        for scope in ("cv", "contact", "section", "entry"):
            assert self._enum(scope, has_entries=True) == self._enum(scope)

    def test_narrowed_enum_stays_deterministic_and_ordered(self):
        a = json.dumps(json_schema_for_cv_chat("cv", has_entries=False), sort_keys=True)
        b = json.dumps(json_schema_for_cv_chat("cv", has_entries=False), sort_keys=True)
        assert a == b

    def test_every_surviving_op_is_satisfiable_without_an_entry_id(self):
        """The invariant behind the drop set, asserted against _REQUIRED_FIELDS itself
        rather than a hand-copied list — so adding an entry-addressing op without
        adding it to _ENTRY_ADDRESSING_OPS fails here."""
        from jsa.schema.chat_turn import _REQUIRED_FIELDS

        for scope in ("cv", "section"):
            for op in self._enum(scope, has_entries=False):
                assert "entry_id" not in _REQUIRED_FIELDS[op], f"{op} still needs an entry_id"

    def test_top_level_envelope_matches_generic_structured_routing(self):
        schema = json_schema_for_cv_chat("cv")
        assert set(schema["properties"]) == {
            "kind",
            "question",
            "payload",
            "suggested_replies",
        }


class TestCvChatTurnEnvelope:
    def test_final_requires_payload(self):
        with pytest.raises(ValidationError):
            CvChatTurn.model_validate(
                {"kind": "final", "question": None, "payload": None, "suggested_replies": None}
            )

    def test_final_valid(self):
        turn = CvChatTurn.model_validate(
            {
                "kind": "final",
                "question": None,
                "payload": {"answer": "Done.", "ops": []},
                "suggested_replies": None,
            }
        )
        assert turn.payload.ops == []

    def test_question_requires_question_text(self):
        with pytest.raises(ValidationError):
            CvChatTurn.model_validate(
                {"kind": "question", "question": None, "payload": None, "suggested_replies": None}
            )
