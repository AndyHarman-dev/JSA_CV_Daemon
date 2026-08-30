"""Tests for jsa.schema.turn_models: strict schema generation + minimal reply parsing.

Covers:
- additionalProperties/required strictness for every model a structured backend's
  schema is built from (CvTurn, ClTurn, FitVerdict) — not just a spot check.
- The kind/payload-iff-final validators (exercised only by these tests; the parse
  path deliberately never calls .model_validate on these models — see the module
  docstring for why).
- parse_structured_reply's json.loads + kind routing, and its ProtocolError cases.
- That a structured "final" reply's content is byte-compatible with the sentinel
  path: feeding it through the same CVDocument/CoverLetter validation the sentinel
  path uses succeeds and produces an equivalent object.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jsa.agents.protocol import ProtocolError
from jsa.db.models import Stage
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument
from jsa.schema.turn_models import (
    STAGE_TURN_MODELS,
    ClTurn,
    CvTurn,
    FitVerdict,
    json_schema_for,
    parse_structured_reply,
)


def _cv_dict(marker: str = "Adjusted CV") -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [{"name": "Summary", "text": f"{marker}: senior engineer."}],
    }


def _cl_dict(marker: str = "motivated") -> dict:
    return {
        "salutation": "Dear Hiring Manager,",
        "paragraphs": [
            f"I am {marker} to apply for this role and bring years of relevant experience. "
            "My background aligns closely with what this position requires, and I would "
            "welcome the chance to contribute to your team's continued success in this area."
        ],
        "signoff": "Best regards,\nJane Doe",
    }


# ---------------------------------------------------------------------------
# Strict-schema assertions — every model a provider schema is built from.
# ---------------------------------------------------------------------------

class TestStrictSchemaShape:
    @pytest.mark.parametrize("model", [CvTurn, ClTurn, FitVerdict])
    def test_additional_properties_false(self, model):
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False

    @pytest.mark.parametrize("model", [CvTurn, ClTurn, FitVerdict])
    def test_every_property_required(self, model):
        schema = model.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"])

    def test_json_schema_for_stage_matches_model(self):
        assert json_schema_for(Stage.cv_adjust) == CvTurn.model_json_schema()
        assert json_schema_for(Stage.revising_cv) == CvTurn.model_json_schema()
        assert json_schema_for(Stage.cover_letter) == ClTurn.model_json_schema()
        assert json_schema_for(Stage.revising_cl) == ClTurn.model_json_schema()
        assert json_schema_for(Stage.fit_assessment) == FitVerdict.model_json_schema()

    def test_stage_turn_models_covers_document_stages(self):
        assert set(STAGE_TURN_MODELS) == {
            Stage.cv_adjust,
            Stage.revising_cv,
            Stage.cover_letter,
            Stage.revising_cl,
        }


# ---------------------------------------------------------------------------
# iff-validators (model-level; never exercised by the parse path itself).
# ---------------------------------------------------------------------------

class TestCvTurnIffValidator:
    def test_final_with_payload_ok(self):
        CvTurn(kind="final", question=None, payload=_cv_dict())

    def test_final_without_payload_rejected(self):
        with pytest.raises(ValidationError):
            CvTurn(kind="final", question=None, payload=None)

    def test_question_with_question_ok(self):
        CvTurn(kind="question", question="Which dates for the last role?", payload=None)

    def test_question_without_question_rejected(self):
        with pytest.raises(ValidationError):
            CvTurn(kind="question", question=None, payload=None)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            CvTurn(kind="final", question=None, payload=_cv_dict(), extra_field="nope")


class TestClTurnIffValidator:
    def test_final_with_payload_ok(self):
        ClTurn(kind="final", question=None, payload=_cl_dict())

    def test_final_without_payload_rejected(self):
        with pytest.raises(ValidationError):
            ClTurn(kind="final", question=None, payload=None)

    def test_question_without_question_rejected(self):
        with pytest.raises(ValidationError):
            ClTurn(kind="question", question=None, payload=None)


class TestFitVerdictRequiredReason:
    def test_missing_reason_rejected(self):
        with pytest.raises(ValidationError):
            FitVerdict(verdict="FIT")

    def test_bare_reason_present_ok(self):
        FitVerdict(verdict="UNFIT", reason="role requires 15+ years, candidate has 4")


# ---------------------------------------------------------------------------
# parse_structured_reply — json.loads + kind routing only.
# ---------------------------------------------------------------------------

class TestParseStructuredReplyCvCl:
    def test_final_reply_routes_to_final_with_reserialized_payload(self):
        cv = _cv_dict()
        raw = json.dumps({"kind": "final", "question": None, "payload": cv})
        reply = parse_structured_reply(raw, Stage.cv_adjust)
        assert reply.kind == "final"
        assert reply.question is None
        assert reply.raw == raw
        # content round-trips through the exact same validation the sentinel path uses.
        assert CVDocument.model_validate(json.loads(reply.content)).contact.name == "Jane Doe"

    def test_question_reply_routes_to_needs_input(self):
        raw = json.dumps({"kind": "question", "question": "Which dates?", "payload": None})
        reply = parse_structured_reply(raw, Stage.cv_adjust)
        assert reply.kind == "needs_input"
        assert reply.question == "Which dates?"
        assert reply.content == "Which dates?"

    def test_cover_letter_final_validates_downstream(self):
        cl = _cl_dict()
        raw = json.dumps({"kind": "final", "question": None, "payload": cl})
        reply = parse_structured_reply(raw, Stage.cover_letter)
        assert reply.kind == "final"
        assert len(CoverLetter.model_validate(json.loads(reply.content)).paragraphs) == 1

    def test_does_not_validate_payload_semantics(self):
        # A payload that would fail CVDocument validation (no sections) is NOT rejected
        # at this layer — semantic validation stays stage-side (_validate_final_content).
        raw = json.dumps({"kind": "final", "question": None, "payload": {"contact": {"name": "X"}}})
        reply = parse_structured_reply(raw, Stage.cv_adjust)
        assert reply.kind == "final"
        assert json.loads(reply.content) == {"contact": {"name": "X"}}

    def test_invalid_json_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            parse_structured_reply("not json{", Stage.cv_adjust)

    def test_missing_kind_raises_protocol_error(self):
        raw = json.dumps({"question": None, "payload": _cv_dict()})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.cv_adjust)

    def test_invalid_kind_raises_protocol_error(self):
        raw = json.dumps({"kind": "bogus"})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.cv_adjust)

    def test_final_missing_payload_raises_protocol_error(self):
        raw = json.dumps({"kind": "final", "question": None, "payload": None})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.cv_adjust)

    def test_question_missing_question_raises_protocol_error(self):
        raw = json.dumps({"kind": "question", "question": None, "payload": None})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.cv_adjust)

    def test_non_object_top_level_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            parse_structured_reply(json.dumps([1, 2, 3]), Stage.cv_adjust)


class TestParseStructuredReplyFit:
    def test_fit_verdict_round_trips_through_existing_parser(self):
        from jsa.pipeline.stages import _parse_fit_verdict

        raw = json.dumps({"verdict": "FIT", "reason": "JD and profile align on backend depth"})
        reply = parse_structured_reply(raw, Stage.fit_assessment)
        assert reply.kind == "final"
        assert reply.content == "FIT\nJD and profile align on backend depth"
        is_fit, reason = _parse_fit_verdict(reply)
        # _parse_fit_verdict discards the reason on a FIT verdict by design (CLAUDE.md
        # → "Fit-assessment gate" / this plan's decision #4) — only UNFIT reasons surface.
        assert is_fit is True
        assert reason is None

    def test_unfit_verdict_round_trips(self):
        from jsa.pipeline.stages import _parse_fit_verdict

        raw = json.dumps({"verdict": "UNFIT", "reason": "requires 15+ years, candidate has 4"})
        reply = parse_structured_reply(raw, Stage.fit_assessment)
        is_fit, reason = _parse_fit_verdict(reply)
        assert is_fit is False
        assert reason == "requires 15+ years, candidate has 4"

    def test_missing_reason_raises_protocol_error(self):
        raw = json.dumps({"verdict": "FIT"})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.fit_assessment)

    def test_invalid_verdict_raises_protocol_error(self):
        raw = json.dumps({"verdict": "MAYBE", "reason": "unsure"})
        with pytest.raises(ProtocolError):
            parse_structured_reply(raw, Stage.fit_assessment)

    def test_non_object_top_level_raises_protocol_error(self):
        with pytest.raises(ProtocolError):
            parse_structured_reply(json.dumps("FIT"), Stage.fit_assessment)
