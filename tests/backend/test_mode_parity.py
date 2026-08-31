"""Phase 6 of the structured-output plan: the equivalence invariant.

This module is a PERMANENT parity gate, not a deletion gate — the sentinel path
lives forever for CLI backends (claude-cli, google-cli). It proves that for the
same LOGICAL payload, a sentinel-mode reply and a structured-mode reply produce
identical observable pipeline outcomes: `Document.structured`, `Document.markdown`,
`Job.fit_reason` + state, and `FollowUp.question` text.

Deliberately does NOT assert on raw `Message` content — `wrap_canonical_for_sentinel`
(jsa/schema/turn_models.py) emits compact JSON where a real model's sentinel-mode
FINAL is usually pretty-printed; the two paths' raw text differs by construction
even when they carry the same logical payload. Only the four observable outputs
listed above are asserted.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Stage
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend


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
async def session_pair(session_factory):
    """Two independent sessions over separate in-memory DBs — one per mode — so the
    two runs can't interfere with each other."""
    async with session_factory() as a, session_factory() as b:
        yield a, b


async def _insert_job(session: AsyncSession, job_id: str) -> Job:
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


_CV_PAYLOAD = {
    "contact": {"name": "Jane Doe", "email": "jane.doe@example.com", "phone": "+1-555-867-5309"},
    "sections": [
        {"name": "Summary", "text": "Parity-gate CV: senior engineer with eight years of experience."},
        {"name": "Experience", "entries": [
            {"role": "Senior Engineer", "company": "Acme", "dates": "2019-present",
             "bullets": ["Built a distributed payment pipeline", "Led a service migration"]},
        ]},
    ],
}

_CL_PAYLOAD = {
    "salutation": "Dear Hiring Manager,",
    "paragraphs": [
        "I am excited to apply because your mission resonates with my four years of "
        "shipping production systems and developer tooling.",
        "I am confident my background aligns well with what your team needs.",
    ],
    "signoff": "Sincerely,\nCandidate Name",
}


def _sentinel_final(payload: dict) -> AgentReply:
    content = json.dumps(payload)
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


def _structured_final(payload: dict) -> AgentReply:
    raw = json.dumps({"kind": "final", "question": None, "payload": payload})
    return AgentReply(raw=raw, content=json.dumps(payload), kind="final")


def _sentinel_question(question: str) -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>", content=question, kind="needs_input", question=question
    )


def _structured_question(question: str) -> AgentReply:
    raw = json.dumps({"kind": "question", "question": question, "payload": None})
    return AgentReply(raw=raw, content=question, kind="needs_input", question=question)


def _sentinel_fit(verdict: str, reason: str) -> AgentReply:
    content = f"{verdict}\n{reason}"
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


def _structured_fit(verdict: str, reason: str) -> AgentReply:
    raw = json.dumps({"verdict": verdict, "reason": reason})
    return AgentReply(raw=raw, content=f"{verdict}\n{reason}", kind="final")


class TestCvAdjustParity:
    async def test_document_structured_and_markdown_are_identical(self, session_pair):
        sentinel_session, structured_session = session_pair
        sentinel_job = await _insert_job(sentinel_session, "parity-cv-sentinel")
        structured_job = await _insert_job(structured_session, "parity-cv-structured")
        transition(sentinel_job, JobState.running, Stage.cv_adjust)
        transition(structured_job, JobState.running, Stage.cv_adjust)
        await sentinel_session.commit()
        await structured_session.commit()

        sentinel_backend = FakeAgentBackend([_sentinel_final(_CV_PAYLOAD)])
        structured_backend = FakeAgentBackend([_structured_final(_CV_PAYLOAD)], supports_structured_output=True)

        await run_stage(sentinel_job, sentinel_backend, Stage.cv_adjust, sentinel_session)
        await run_stage(structured_job, structured_backend, Stage.cv_adjust, structured_session)

        sentinel_docs = await repo.get_documents(sentinel_session, sentinel_job.id, stage=Stage.cv_adjust)
        structured_docs = await repo.get_documents(structured_session, structured_job.id, stage=Stage.cv_adjust)

        assert sentinel_docs[0].structured == structured_docs[0].structured
        assert sentinel_docs[0].markdown == structured_docs[0].markdown

        refreshed_sentinel = await repo.get_job(sentinel_session, sentinel_job.id)
        refreshed_structured = await repo.get_job(structured_session, structured_job.id)
        assert refreshed_sentinel.state == refreshed_structured.state == JobState.cv_review

    async def test_followup_question_text_is_identical(self, session_pair):
        sentinel_session, structured_session = session_pair
        sentinel_job = await _insert_job(sentinel_session, "parity-q-sentinel")
        structured_job = await _insert_job(structured_session, "parity-q-structured")
        transition(sentinel_job, JobState.running, Stage.cv_adjust)
        transition(structured_job, JobState.running, Stage.cv_adjust)
        await sentinel_session.commit()
        await structured_session.commit()

        question = "Which dates should I use for the last role?"
        sentinel_backend = FakeAgentBackend([_sentinel_question(question)])
        structured_backend = FakeAgentBackend([_structured_question(question)], supports_structured_output=True)

        from jsa.pipeline.stages import PausedForInput

        with pytest.raises(PausedForInput):
            await run_stage(sentinel_job, sentinel_backend, Stage.cv_adjust, sentinel_session)
        with pytest.raises(PausedForInput):
            await run_stage(structured_job, structured_backend, Stage.cv_adjust, structured_session)

        sentinel_fu = (await sentinel_session.execute(
            select(FollowUp).where(FollowUp.job_id == sentinel_job.id)
        )).scalar_one()
        structured_fu = (await structured_session.execute(
            select(FollowUp).where(FollowUp.job_id == structured_job.id)
        )).scalar_one()

        assert sentinel_fu.question == structured_fu.question == question


class TestCoverLetterParity:
    async def test_document_structured_and_markdown_are_identical(self, session_pair):
        sentinel_session, structured_session = session_pair
        sentinel_job = await _insert_job(sentinel_session, "parity-cl-sentinel")
        structured_job = await _insert_job(structured_session, "parity-cl-structured")
        for job, s in ((sentinel_job, sentinel_session), (structured_job, structured_session)):
            transition(job, JobState.running, Stage.cv_adjust)
            transition(job, JobState.cv_review, None)
            transition(job, JobState.cv_done, None)
            transition(job, JobState.running, Stage.cover_letter)
            await s.commit()

        sentinel_backend = FakeAgentBackend([_sentinel_final(_CL_PAYLOAD)])
        structured_backend = FakeAgentBackend([_structured_final(_CL_PAYLOAD)], supports_structured_output=True)

        await run_stage(sentinel_job, sentinel_backend, Stage.cover_letter, sentinel_session)
        await run_stage(structured_job, structured_backend, Stage.cover_letter, structured_session)

        sentinel_docs = await repo.get_documents(sentinel_session, sentinel_job.id, stage=Stage.cover_letter)
        structured_docs = await repo.get_documents(structured_session, structured_job.id, stage=Stage.cover_letter)

        assert sentinel_docs[0].structured == structured_docs[0].structured
        assert sentinel_docs[0].markdown == structured_docs[0].markdown


class TestFitVerdictParity:
    async def test_fit_verdict_state_and_reason_are_identical(self, session_pair):
        """FIT direction: job.fit_reason is discarded (None) on both paths — see
        _run_fit_assessment's `job.fit_reason = None if is_fit else reason`."""
        sentinel_session, structured_session = session_pair
        sentinel_job = await _insert_job(sentinel_session, "parity-fit-sentinel")
        structured_job = await _insert_job(structured_session, "parity-fit-structured")
        transition(sentinel_job, JobState.running, Stage.fit_assessment)
        transition(structured_job, JobState.running, Stage.fit_assessment)
        await sentinel_session.commit()
        await structured_session.commit()

        reason = "JD and candidate profile align on distributed-systems experience."
        sentinel_backend = FakeAgentBackend([_sentinel_fit("FIT", reason)])
        structured_backend = FakeAgentBackend([_structured_fit("FIT", reason)], supports_structured_output=True)

        await run_stage(sentinel_job, sentinel_backend, Stage.fit_assessment, sentinel_session)
        await run_stage(structured_job, structured_backend, Stage.fit_assessment, structured_session)

        refreshed_sentinel = await repo.get_job(sentinel_session, sentinel_job.id)
        refreshed_structured = await repo.get_job(structured_session, structured_job.id)
        assert refreshed_sentinel.state == refreshed_structured.state == JobState.fit_done
        assert refreshed_sentinel.fit_reason is None
        assert refreshed_structured.fit_reason is None

    async def test_unfit_verdict_reason_text_is_identical(self, session_pair):
        """UNFIT direction: this is the one where fit_reason is actually persisted —
        the two paths must agree on the exact stored reason text, not just the state."""
        sentinel_session, structured_session = session_pair
        sentinel_job = await _insert_job(sentinel_session, "parity-unfit-sentinel")
        structured_job = await _insert_job(structured_session, "parity-unfit-structured")
        transition(sentinel_job, JobState.running, Stage.fit_assessment)
        transition(structured_job, JobState.running, Stage.fit_assessment)
        await sentinel_session.commit()
        await structured_session.commit()

        reason = "Role requires 15+ years of UI experience; candidate has only 4."
        sentinel_backend = FakeAgentBackend([_sentinel_fit("UNFIT", reason)])
        structured_backend = FakeAgentBackend([_structured_fit("UNFIT", reason)], supports_structured_output=True)

        await run_stage(sentinel_job, sentinel_backend, Stage.fit_assessment, sentinel_session)
        await run_stage(structured_job, structured_backend, Stage.fit_assessment, structured_session)

        refreshed_sentinel = await repo.get_job(sentinel_session, sentinel_job.id)
        refreshed_structured = await repo.get_job(structured_session, structured_job.id)
        assert refreshed_sentinel.state == refreshed_structured.state == JobState.unfit
        assert refreshed_sentinel.fit_reason == refreshed_structured.fit_reason == reason
