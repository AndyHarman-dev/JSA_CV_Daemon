"""Tests for the mixed-mode history replay adapter (jsa.schema.turn_models).

Two layers:
1. Unit tests for the primitives (_is_sentinel_wrapped / wrap_canonical_for_sentinel /
   unwrap_sentinel_to_canonical / adapt_history) — content-based per-row detection,
   never-raises guarantee, and the "everything today is already sentinel-wrapped, so
   this is currently a no-op end to end" invariant.
2. Integration tests exercising each of the three restore_session call sites in
   jsa/pipeline/stages.py enumerated by the structured-output plan's Phase 2: fresh/
   resume cv_adjust & cover_letter, and revising_cv/revising_cl (both the fresh-
   revision and the mid-revision-resume branch) — proving canonical (structured-mode-
   shaped) Message rows get sentinel-wrapped before being handed to a sentinel-mode
   destination backend via restore_session.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import HistoryTurn
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Message, RevisionRequest, Stage
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from jsa.schema.turn_models import (
    _is_sentinel_wrapped,
    adapt_history,
    unwrap_sentinel_to_canonical,
    wrap_canonical_for_sentinel,
)
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cv_final


def _canonical_final(payload: dict) -> str:
    return json.dumps({"kind": "final", "question": None, "payload": payload})


def _canonical_question(question: str) -> str:
    return json.dumps({"kind": "question", "question": question, "payload": None})


_CV_PAYLOAD = {
    "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
    "sections": [{"name": "Summary", "text": "Canonical row: senior engineer."}],
}


# ---------------------------------------------------------------------------
# Unit tests — primitives
# ---------------------------------------------------------------------------


class TestIsSentinelWrapped:
    def test_sentinel_final_is_wrapped(self):
        assert _is_sentinel_wrapped("<<<FINAL>>>\n{}\n<<<END>>>")

    def test_canonical_json_is_not_wrapped(self):
        assert not _is_sentinel_wrapped('{"kind": "final"}')

    def test_plain_text_is_not_wrapped(self):
        assert not _is_sentinel_wrapped("FIT\nreason text")


class TestWrapCanonicalForSentinel:
    def test_wraps_final_payload(self):
        raw = _canonical_final(_CV_PAYLOAD)
        wrapped = wrap_canonical_for_sentinel(raw)
        assert wrapped == f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"

    def test_wraps_question(self):
        raw = _canonical_question("Which dates for the last role?")
        wrapped = wrap_canonical_for_sentinel(raw)
        assert wrapped == "<<<NEED_INPUT>>>\nWhich dates for the last role?\n<<<END>>>"

    def test_already_sentinel_wrapped_passes_through(self):
        raw = "<<<FINAL>>>\n{}\n<<<END>>>"
        assert wrap_canonical_for_sentinel(raw) == raw

    def test_non_json_text_passes_through_unchanged(self):
        raw = "FIT\nrole requires more experience"
        assert wrap_canonical_for_sentinel(raw) == raw

    def test_json_array_passes_through_unchanged(self):
        raw = json.dumps([1, 2, 3])
        assert wrap_canonical_for_sentinel(raw) == raw

    def test_unrecognized_kind_passes_through_unchanged(self):
        raw = json.dumps({"kind": "bogus"})
        assert wrap_canonical_for_sentinel(raw) == raw


class TestUnwrapSentinelToCanonical:
    def test_unwraps_final_to_canonical(self):
        raw = f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"
        canonical = unwrap_sentinel_to_canonical(raw)
        assert json.loads(canonical) == {"kind": "final", "question": None, "payload": _CV_PAYLOAD}

    def test_unwraps_need_input_to_canonical(self):
        raw = "<<<NEED_INPUT>>>\nWhich dates?\n<<<END>>>"
        canonical = unwrap_sentinel_to_canonical(raw)
        assert json.loads(canonical) == {
            "kind": "question", "question": "Which dates?", "payload": None,
        }

    def test_already_canonical_passes_through_unchanged(self):
        raw = _canonical_final(_CV_PAYLOAD)
        assert unwrap_sentinel_to_canonical(raw) == raw

    def test_fit_shaped_final_never_raises_passes_through(self):
        # A fit_assessment row ("FIT\n<reason>") is not JSON — must never raise.
        raw = "<<<FINAL>>>\nFIT\nJD and profile align well\n<<<END>>>"
        assert unwrap_sentinel_to_canonical(raw) == raw

    def test_unterminated_sentinel_never_raises_passes_through(self):
        raw = "<<<FINAL>>>\nno closing marker"
        assert unwrap_sentinel_to_canonical(raw) == raw

    def test_round_trip_final(self):
        raw = f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"
        assert wrap_canonical_for_sentinel(unwrap_sentinel_to_canonical(raw)) == raw

    def test_round_trip_question(self):
        raw = "<<<NEED_INPUT>>>\nWhich dates?\n<<<END>>>"
        assert wrap_canonical_for_sentinel(unwrap_sentinel_to_canonical(raw)) == raw


class TestAdaptHistory:
    def test_user_turns_never_touched(self):
        history = [HistoryTurn(role="user", content=_canonical_final(_CV_PAYLOAD))]
        adapted = adapt_history(history, structured=False)
        assert adapted[0].content == history[0].content

    def test_structured_false_wraps_canonical_assistant_turns(self):
        history = [
            HistoryTurn(role="user", content="hello"),
            HistoryTurn(role="assistant", content=_canonical_final(_CV_PAYLOAD)),
        ]
        adapted = adapt_history(history, structured=False)
        assert adapted[0].content == "hello"
        assert adapted[1].content == f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"

    def test_structured_true_unwraps_sentinel_assistant_turns(self):
        wrapped = f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"
        history = [HistoryTurn(role="assistant", content=wrapped)]
        adapted = adapt_history(history, structured=True)
        assert json.loads(adapted[0].content) == {
            "kind": "final", "question": None, "payload": _CV_PAYLOAD,
        }

    def test_todays_all_sentinel_history_is_a_no_op_into_sentinel_destination(self):
        # Every row in production today is already sentinel-wrapped — proving the
        # adapter is currently inert end to end.
        history = [
            HistoryTurn(role="user", content="answer text"),
            HistoryTurn(role="assistant", content="<<<FINAL>>>\n{\"a\": 1}\n<<<END>>>"),
        ]
        adapted = adapt_history(history, structured=False)
        assert [t.content for t in adapted] == [t.content for t in history]


# ---------------------------------------------------------------------------
# Integration tests — the three restore_session call sites in stages.py
# ---------------------------------------------------------------------------


class _CapturingRestoreBackend(FakeAgentBackend):
    """Records every `history` list passed to restore_session."""

    def __init__(self, replies):
        super().__init__(replies)
        self.restore_histories: list[list[HistoryTurn]] = []

    async def restore_session(self, system_prompt, history, external_id):
        self.restore_histories.append(list(history))
        return FakeSessionHandle(id="fake-restore", external_id=external_id)


@pytest.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


async def _insert_job(session: AsyncSession, job_id: str = "replayadapt00001") -> Job:
    data = dict(
        id=job_id,
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
        cv_text="Curriculum vitae text",
    )
    job = await repo.upsert_job(session, data)
    job.state = JobState.pending
    await session.commit()
    return job


class TestCvAdjustResumeCallSite:
    """Call site 1: fresh/resume cv_adjust (and cover_letter, same code path)."""

    async def test_canonical_assistant_row_is_sentinel_wrapped_for_resume(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # A canonical (structured-mode-shaped) assistant row, as if produced by a prior
        # structured-mode turn before a BF-19 switch to a sentinel-only backend.
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV",
        ))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=_canonical_final(_CV_PAYLOAD),
        ))
        session.add(FollowUp(
            job_id=job.id, stage=Stage.cv_adjust, question="q?", answer="a!",
        ))
        await session.commit()
        # Mark the FollowUp answered (answered_at is what _get_latest_answer requires).
        from datetime import datetime

        result = await session.execute(
            FollowUp.__table__.update()
            .where(FollowUp.job_id == job.id)
            .values(answered_at=datetime.utcnow())
        )
        await session.commit()

        backend = _CapturingRestoreBackend([cv_final("Resumed")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert len(backend.restore_histories) == 1
        assistant_turns = [t for t in backend.restore_histories[0] if t.role == "assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0].content == f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"


async def _add_revision_request(session, job_id, target, instruction="Make it shorter"):
    rr = RevisionRequest(job_id=job_id, target=target, instruction=instruction, consumed_at=None)
    session.add(rr)
    await session.commit()
    return rr


class TestRevisingCvFreshCallSite:
    """Call site 2: revising_cv fresh revision — loads original_stage (cv_adjust)
    history, which may hold canonical rows from a prior structured-mode cv_adjust run."""

    async def test_original_stage_canonical_row_is_sentinel_wrapped(self, session):
        job = await _insert_job(session)

        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV",
        ))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=_canonical_final(_CV_PAYLOAD),
        ))
        await session.commit()

        await _add_revision_request(session, job.id, Stage.cv_adjust)
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = _CapturingRestoreBackend([cv_final("Revised")])
        await run_stage(job, backend, Stage.revising_cv, session)

        assert len(backend.restore_histories) == 1
        assistant_turns = [t for t in backend.restore_histories[0] if t.role == "assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0].content == f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"


class TestRevisingCvResumeCallSite:
    """Call site 3 (the plan's specifically-called-out gap): a revising_cv mid-revision
    resume combines original_stage history + revision_turns — both may hold canonical
    rows (e.g. an anthropic-structured cv_adjust run, later BF-19-switched to
    claude-cli for the revision itself)."""

    async def test_combined_history_is_sentinel_wrapped_on_resume(self, session):
        job = await _insert_job(session)

        # Original cv_adjust session — canonical (structured-mode) row.
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV",
        ))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=_canonical_final(_CV_PAYLOAD),
        ))
        await session.commit()

        rev_req = await _add_revision_request(session, job.id, Stage.cv_adjust)

        # Revision turns: a question was asked (canonical) and answered — simulating a
        # revision that started on a structured backend, then parked for awaiting_input.
        session.add(Message(
            job_id=job.id, stage=Stage.revising_cv, role="user", content="Make it shorter",
        ))
        session.add(Message(
            job_id=job.id, stage=Stage.revising_cv, role="assistant",
            content=_canonical_question("Should I drop the oldest role entirely?"),
        ))
        await session.commit()

        from datetime import datetime, timedelta

        session.add(FollowUp(
            job_id=job.id, stage=Stage.revising_cv,
            question="Should I drop the oldest role entirely?", answer="Yes, drop it.",
            answered_at=rev_req.created_at + timedelta(seconds=1),
        ))
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = _CapturingRestoreBackend([cv_final("Revised again")])
        await run_stage(job, backend, Stage.revising_cv, session)

        assert len(backend.restore_histories) == 1
        history = backend.restore_histories[0]
        assistant_turns = [t for t in history if t.role == "assistant"]
        assert len(assistant_turns) == 2
        assert assistant_turns[0].content == f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>"
        assert assistant_turns[1].content == (
            "<<<NEED_INPUT>>>\nShould I drop the oldest role entirely?\n<<<END>>>"
        )
