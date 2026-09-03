"""Tests for jsa.api.transcript.build_transcript — pure projection, no DB needed."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from jsa.api.transcript import build_transcript
from jsa.db.models import Document, FollowUp, Job, JobState, Message, RevisionRequest, Stage

BASE = datetime(2026, 1, 1, 12, 0, 0)


def _job(**overrides) -> Job:
    defaults = dict(
        id="job0000deadbeef",
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="JD text",
        jd_hash="hash0000",
        cv_text="",
        state=JobState.awaiting_input,
        current_stage=Stage.cv_adjust,
        created_at=BASE,
        updated_at=BASE,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _msg(id: int, stage: Stage, role: str, content: str, created_at: datetime) -> Message:
    m = Message(id=id, job_id="job0000deadbeef", stage=stage, role=role, content=content, created_at=created_at)
    return m


def _fu(id: int, stage: Stage, question: str, asked_at: datetime, answer=None, answered_at=None) -> FollowUp:
    return FollowUp(
        id=id,
        job_id="job0000deadbeef",
        stage=stage,
        question=question,
        answer=answer,
        asked_at=asked_at,
        answered_at=answered_at,
    )


def _doc(id: int, stage: Stage, version: int, created_at: datetime) -> Document:
    return Document(
        id=id,
        job_id="job0000deadbeef",
        stage=stage,
        version=version,
        markdown="# doc",
        structured=None,
        created_at=created_at,
    )


def _rev(id: int, target: Stage, instruction: str, origin_state: str = "review") -> RevisionRequest:
    return RevisionRequest(
        id=id,
        job_id="job0000deadbeef",
        target=target,
        instruction=instruction,
        origin_state=origin_state,
    )


class TestSystemRowsExcluded:
    def test_system_message_dropped(self):
        job = _job(fit_reason=None)
        messages = [
            _msg(1, Stage.cv_adjust, "system", "the whole system prompt", BASE),
            _msg(2, Stage.cv_adjust, "user", "hello", BASE + timedelta(seconds=1)),
        ]
        turns = build_transcript(job, messages, [], [], [])
        assert all(t["kind"] != "plumbing" or "system prompt" not in t["text"] for t in turns)
        kinds = [(t["role"], t["text"]) for t in turns]
        assert ("system", "the whole system prompt") not in [(r, t) for r, t in kinds]
        # only the user message survives, as plumbing (not folded by any rule)
        assert len(turns) == 1
        assert turns[0]["kind"] == "plumbing"
        assert turns[0]["role"] == "user"


class TestStructuredAssistantRowUnwrapped:
    def test_structured_json_envelope_not_leaked_raw(self):
        job = _job(fit_reason=None)
        raw = json.dumps({"kind": "question", "question": "What is your salary range?", "payload": None})
        messages = [_msg(1, Stage.cv_adjust, "assistant", raw, BASE)]
        turns = build_transcript(job, messages, [], [], [])
        assert len(turns) == 1
        t = turns[0]
        assert t["kind"] == "plumbing"
        # It IS the (re-serialized) structured envelope, not a "question" turn —
        # questions come exclusively from FollowUp rows, never Message rows.
        assert t["kind"] != "question"
        data = json.loads(t["text"])
        assert data["kind"] == "question"


class TestAnsweredFollowUp:
    def test_yields_question_and_answer_turns(self):
        job = _job(fit_reason=None)
        fu = _fu(
            1,
            Stage.cv_adjust,
            "What is your notice period?",
            BASE,
            answer="Two weeks",
            answered_at=BASE + timedelta(seconds=5),
        )
        turns = build_transcript(job, [], [fu], [], [])
        kinds = [t["kind"] for t in turns]
        assert kinds == ["question", "answer"]
        assert turns[0]["text"] == "What is your notice period?"
        assert turns[0]["follow_up_id"] == 1
        assert turns[1]["text"] == "Two weeks"
        assert turns[1]["follow_up_id"] == 1
        assert turns[1]["role"] == "user"


class TestOrderingStability:
    def test_same_checkpoint_batch_stable(self):
        """Three messages written in one checkpoint share a near-identical timestamp;
        id must be the deciding tiebreaker."""
        job = _job(fit_reason=None)
        t0 = BASE
        messages = [
            _msg(3, Stage.cv_adjust, "assistant", "third", t0),
            _msg(1, Stage.cv_adjust, "user", "first", t0),
            _msg(2, Stage.cv_adjust, "assistant", "second", t0),
        ]
        turns = build_transcript(job, messages, [], [], [])
        assert [t["text"] for t in turns] == ["first", "second", "third"]

    def test_stable_across_repeated_calls(self):
        job = _job(fit_reason=None)
        t0 = BASE
        messages = [
            _msg(3, Stage.cv_adjust, "assistant", "third", t0),
            _msg(1, Stage.cv_adjust, "user", "first", t0),
            _msg(2, Stage.cv_adjust, "assistant", "second", t0),
        ]
        first = build_transcript(job, messages, [], [], [])
        second = build_transcript(job, messages, [], [], [])
        assert first == second


class TestWireRetryDedupe:
    def test_answer_containment_folds_user_message(self):
        job = _job(fit_reason=None)
        fu = _fu(
            1,
            Stage.cv_adjust,
            "What is your notice period?",
            BASE,
            answer="Two weeks",
            answered_at=BASE + timedelta(seconds=5),
        )
        # resend at resume — verbatim
        messages = [_msg(1, Stage.cv_adjust, "user", "Two weeks", BASE + timedelta(seconds=6))]
        turns = build_transcript(job, messages, [fu], [], [])
        kinds = [t["kind"] for t in turns]
        assert kinds == ["question", "answer"]  # the Message row produced no extra turn

    def test_structured_wire_correction_wrapped_instruction_is_answer(self):
        """_STRUCTURED_WIRE_CORRECTION.format(original=text) wraps the original
        instruction — containment, not equality, must still classify it as an
        'answer' turn, never 'plumbing'."""
        job = _job(fit_reason=None)
        instruction = "Please emphasize my leadership experience more."
        wrapped = (
            "Your previous reply could not be parsed as a valid structured object — it "
            "must be a single JSON object with a `kind` field...\n\n" + instruction
        )
        rev = _rev(1, Stage.cv_adjust, instruction)
        messages = [_msg(1, Stage.revising_cv, "user", wrapped, BASE)]
        turns = build_transcript(job, messages, [], [], [rev])
        assert len(turns) == 1
        assert turns[0]["kind"] == "answer"
        assert turns[0]["role"] == "user"
        assert turns[0]["text"] == instruction

    def test_self_heal_correction_stays_plumbing(self):
        """_self_heal_final's own user rows contain neither an answer nor an
        instruction, so they correctly stay plumbing."""
        job = _job(fit_reason=None)
        correction_text = "Your previous <<<FINAL>>> block was not a valid CV JSON object. Re-emit now..."
        messages = [_msg(1, Stage.cv_adjust, "user", correction_text, BASE)]
        turns = build_transcript(job, messages, [], [], [])
        assert len(turns) == 1
        assert turns[0]["kind"] == "plumbing"


class TestVerdict:
    def test_verdict_turn_present_when_fit_reason_set(self):
        job = _job(fit_reason="Strong match on required skills.")
        fu = _fu(1, Stage.cv_adjust, "Q?", BASE + timedelta(seconds=10))
        turns = build_transcript(job, [], [fu], [], [])
        kinds = [t["kind"] for t in turns]
        assert kinds[0] == "verdict"
        assert turns[0]["stage"] == "fit_assessment"
        assert turns[0]["text"] == "Strong match on required skills."

    def test_no_verdict_turn_when_fit_reason_unset(self):
        job = _job(fit_reason=None)
        turns = build_transcript(job, [], [], [], [])
        assert turns == []


class TestDelivery:
    def test_delivery_marker_per_document(self):
        job = _job(fit_reason=None)
        doc = _doc(1, Stage.cv_adjust, 1, BASE)
        turns = build_transcript(job, [], [], [doc], [])
        assert len(turns) == 1
        assert turns[0]["kind"] == "delivery"
        assert turns[0]["text"] == "Delivered CV v1"
        # never the document body
        assert "# doc" not in turns[0]["text"]


class TestPostRevisionChronology:
    def test_revised_cv_delivery_sorts_after_cover_letter_turns(self):
        """A revising_cv Document is stored under its anchor stage (cv_adjust) —
        stage_index alone would misorder it before the cover_letter conversation
        that (chronologically) preceded it. Delivery sorts by timestamp first."""
        job = _job(fit_reason=None)
        t_cv_delivery_v1 = BASE
        t_cl_question = BASE + timedelta(minutes=5)
        t_cl_answer = BASE + timedelta(minutes=6)
        t_cv_revision_delivery_v2 = BASE + timedelta(minutes=10)

        documents = [
            _doc(1, Stage.cv_adjust, 1, t_cv_delivery_v1),
            _doc(2, Stage.cv_adjust, 2, t_cv_revision_delivery_v2),  # revising_cv's doc, anchored to cv_adjust
        ]
        fu = _fu(
            1,
            Stage.cover_letter,
            "Any specific achievements to highlight?",
            t_cl_question,
            answer="Yes, the Q3 launch.",
            answered_at=t_cl_answer,
        )
        turns = build_transcript(job, [], [fu], documents, [])
        texts_in_order = [t["text"] for t in turns]
        assert texts_in_order.index("Delivered CV v1") < texts_in_order.index(
            "Any specific achievements to highlight?"
        )
        assert texts_in_order.index("Yes, the Q3 launch.") < texts_in_order.index("Delivered CV v2")
