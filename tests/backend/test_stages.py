"""Unit tests for jsa.pipeline.stages — run_stage function.

Uses FakeAgentBackend (scripted replies) and in-memory SQLite.
All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle
from jsa.db import repo
from jsa.db.models import (
    Base,
    Document,
    FollowUp,
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.stages import PausedForInput, _validate_cl_content, run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    """In-memory SQLite with StaticPool so all sessions share the same DB."""
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
    """Yield a single AsyncSession for most tests."""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_data(
    job_id: str = "aabbccdd00112233",
    company: str = "Acme",
    role: str = "Engineer",
    link: str = "https://acme.com/job",
    tier: str = "A",
    jd: str = "Job description text",
    jd_hash: str = "hash0000deadbeef",
    cv_text: str = "Curriculum vitae text",
) -> dict:
    return dict(
        id=job_id,
        company=company,
        role=role,
        link=link,
        tier=tier,
        jd=jd,
        jd_hash=jd_hash,
        cv_text=cv_text,
    )


async def _insert_job(session: AsyncSession, **overrides) -> Job:
    """Insert a job using upsert_job and commit."""
    data = _job_data(**overrides)
    job = await repo.upsert_job(session, data)
    await session.commit()
    return job


def _final_reply(content: str = "# Adjusted CV\n\nThis is the adjusted CV.") -> AgentReply:
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


# ---------------------------------------------------------------------------
# FakeAgentBackend with traceable external_id (for session_external_id tests)
# ---------------------------------------------------------------------------


class FakeBackendWithExternalId(FakeAgentBackend):
    """FakeAgentBackend that produces handles with a non-None external_id."""

    EXTERNAL_ID = "fake-session-123"

    async def start_session(self, system_prompt, initial_user_msg):
        handle = FakeSessionHandle(id=str(uuid4()), external_id=self.EXTERNAL_ID)
        reply = self._pop_reply()
        return handle, reply


# ---------------------------------------------------------------------------
# Test: Happy path — cv_adjust → cv_done
# ---------------------------------------------------------------------------


class TestCvAdjustHappyPath:
    async def test_job_transitions_to_cv_done(self, session):
        """Fresh cv_adjust session: FINAL reply → job in cv_done."""
        job = await _insert_job(session)
        # Transition to running(cv_adjust) before calling run_stage
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# My Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_done
        assert refreshed.current_stage is None

    async def test_document_written_with_cv_adjust_stage(self, session):
        """Document row created with stage=cv_adjust after successful run."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# My Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 1
        assert docs[0].stage == Stage.cv_adjust
        assert docs[0].version == 1
        assert "My Adjusted CV" in docs[0].markdown

    async def test_messages_written_after_cv_adjust(self, session):
        """system, user, assistant Message rows are written after cv_adjust."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session)

        result = await session.execute(
            select(Message).where(Message.job_id == job.id).order_by(Message.id.asc())
        )
        msgs = list(result.scalars().all())
        roles = [m.role for m in msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Test: Happy path — cover_letter → cl_done → review
# ---------------------------------------------------------------------------


class TestCoverLetterHappyPath:
    async def test_job_transitions_to_review_after_cover_letter(self, session):
        """cover_letter FINAL reply → job advances to review (not cl_done, which is transient)."""
        job = await _insert_job(session)
        # Simulate that cv_adjust is done; job is now cv_done
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        # Now transition to running(cover_letter)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_final_reply(_REAL_LETTER)])
        await run_stage(job, backend, Stage.cover_letter, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage is None

    async def test_document_written_with_cover_letter_stage(self, session):
        """Document row created with stage=cover_letter after successful run."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_final_reply(_REAL_LETTER)])
        await run_stage(job, backend, Stage.cover_letter, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cover_letter)
        assert len(docs) == 1
        assert docs[0].stage == Stage.cover_letter
        assert docs[0].version == 1

    async def test_messages_written_after_cover_letter(self, session):
        """system, user, assistant Message rows are written for cover_letter stage."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_final_reply(_REAL_LETTER)])
        await run_stage(job, backend, Stage.cover_letter, session)

        result = await session.execute(
            select(Message)
            .where(Message.job_id == job.id, Message.stage == Stage.cover_letter)
            .order_by(Message.id.asc())
        )
        msgs = list(result.scalars().all())
        roles = [m.role for m in msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Test: Needs input → park to awaiting_input
# ---------------------------------------------------------------------------


class TestNeedsInputParking:
    async def test_paused_for_input_raised(self, session):
        """run_stage raises PausedForInput when agent returns needs_input."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

    async def test_job_transitions_to_awaiting_input(self, session):
        """After PausedForInput, job state is awaiting_input in DB."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.awaiting_input
        assert refreshed.current_stage == Stage.cv_adjust

    async def test_follow_up_row_inserted(self, session):
        """A FollowUp row is written with the question text when parking."""
        question = "What is your target role?"
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply(question)])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        fus = await repo.get_follow_ups(session, job.id, answered=False)
        assert len(fus) == 1
        assert fus[0].question == question
        assert fus[0].answered_at is None

    async def test_session_external_id_set_on_job(self, session):
        """session_external_id is stored on the job when parking (non-None external_id)."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeBackendWithExternalId([_needs_input_reply("What tier?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.session_external_id == FakeBackendWithExternalId.EXTERNAL_ID


# ---------------------------------------------------------------------------
# Test: Resume after answer
# ---------------------------------------------------------------------------


class TestResumeAfterAnswer:
    async def test_resume_continues_to_cv_done(self, session):
        """Resume after answered FollowUp continues to completion."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: park with needs_input
        backend1 = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        # Simulate user answering: insert FollowUp answer
        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fu = fus[0]
        fu.answer = "Software Engineer"
        fu.answered_at = datetime.utcnow()
        await session.commit()

        # Transition back to running for the resume
        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Second run: backend returns FINAL
        backend2 = FakeAgentBackend([_final_reply("# Adjusted CV after answer")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_done

    async def test_resume_writes_only_new_messages(self, session):
        """On resume, only the new answer + reply messages are added (not re-written)."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: park
        backend1 = FakeAgentBackend([_needs_input_reply("Role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        msgs_after_park_result = await session.execute(
            select(Message).where(Message.job_id == job.id)
        )
        msgs_after_park = list(msgs_after_park_result.scalars().all())
        count_before_resume = len(msgs_after_park)

        # Answer and resume
        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fus[0].answer = "SWE"
        fus[0].answered_at = datetime.utcnow()
        await session.commit()

        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend2 = FakeAgentBackend([_final_reply("# Final CV")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        msgs_after_resume_result = await session.execute(
            select(Message).where(Message.job_id == job.id)
        )
        msgs_after_resume = list(msgs_after_resume_result.scalars().all())
        # Two new messages: user answer and assistant final reply
        assert len(msgs_after_resume) == count_before_resume + 2


# ---------------------------------------------------------------------------
# Test: Revision flow — revising_cv
# ---------------------------------------------------------------------------


class TestRevisionFlow:
    async def _setup_job_in_review(self, session: AsyncSession) -> Job:
        """Helper: create a job in review state with a cv_adjust Document."""
        job = await _insert_job(session)
        # Set up state as if cv_adjust and cover_letter completed
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done, None)
        transition(job, JobState.review, None)
        await session.commit()

        # Insert initial CV document and some message history for cv_adjust
        doc = Document(
            job_id=job.id,
            stage=Stage.cv_adjust,
            version=1,
            markdown="# Original CV",
        )
        session.add(doc)
        # Add prior messages for cv_adjust (needed by restore_session history load)
        msg_user = Message(
            job_id=job.id,
            stage=Stage.cv_adjust,
            role="user",
            content="Initial CV text",
        )
        msg_asst = Message(
            job_id=job.id,
            stage=Stage.cv_adjust,
            role="assistant",
            content="# Original CV",
        )
        session.add(msg_user)
        session.add(msg_asst)
        # Set per-stage session IDs so the BF-9 null-session guard does not fire.
        job.cv_session_id = "fake-session-cv-123"
        job.cl_session_id = "fake-session-cl-123"
        await session.commit()
        return job

    async def test_revision_produces_new_document_version(self, session):
        """revising_cv: FINAL → new Document version written."""
        job = await self._setup_job_in_review(session)

        # Insert RevisionRequest
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        # Transition to running(revising_cv)
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 2  # original + revised
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2]

    async def test_revision_returns_to_review_state(self, session):
        """revising_cv: after FINAL, job returns to review state."""
        job = await self._setup_job_in_review(session)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage is None

    async def test_revision_consumes_revision_request(self, session):
        """revising_cv: RevisionRequest.consumed_at is set after completion."""
        job = await self._setup_job_in_review(session)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        rr_refreshed = result.scalar_one()
        assert rr_refreshed.consumed_at is not None


# ---------------------------------------------------------------------------
# Test: Document versioning
# ---------------------------------------------------------------------------


class TestDocumentVersioning:
    async def test_two_revisions_produce_version_1_and_2(self, session):
        """Running cv_adjust twice (via revision) yields versions 1 and 2."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: fresh cv_adjust → version 1
        backend1 = FakeAgentBackend([_final_reply("# CV v1")])
        await run_stage(job, backend1, Stage.cv_adjust, session)

        docs_v1 = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs_v1) == 1
        assert docs_v1[0].version == 1

        # Now simulate revision: set up review state and insert RevisionRequest
        # Need to go through cover_letter to reach review state
        job_after_cv = await repo.get_job(session, job.id)
        transition(job_after_cv, JobState.running, Stage.cover_letter)
        transition(job_after_cv, JobState.cl_done, None)
        transition(job_after_cv, JobState.review, None)
        await session.commit()

        # Add message history for cv_adjust (required for restore_session in revision)
        msg_user = Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial"
        )
        msg_asst = Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant", content="# CV v1"
        )
        session.add(msg_user)
        session.add(msg_asst)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Expand the skills section",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        job_for_revision = await repo.get_job(session, job.id)
        # Set per-stage session IDs so the BF-9 null-session guard does not fire.
        job_for_revision.cv_session_id = "fake-session-cv-123"
        job_for_revision.cl_session_id = "fake-session-cl-123"
        transition(job_for_revision, JobState.running, Stage.revising_cv)
        await session.commit()

        backend2 = FakeAgentBackend([_final_reply("# CV v2 (expanded)")])
        await run_stage(job_for_revision, backend2, Stage.revising_cv, session)

        docs_v2 = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs_v2) == 2
        versions = sorted(d.version for d in docs_v2)
        assert versions == [1, 2]
        latest = max(docs_v2, key=lambda d: d.version)
        assert "CV v2" in latest.markdown


# ---------------------------------------------------------------------------
# _validate_cl_content unit tests
# ---------------------------------------------------------------------------

_REAL_LETTER = """\
Dear Hiring Manager,

I am writing to express my strong interest in the Developer Relations role at Rive. \
Having spent the last four years building real-time graphics tooling with Unreal Engine's \
Blueprint and native C++ layers, I am drawn to Rive's mission of making interactive \
animation accessible to every developer on every platform.

My work on Promise/Future async wrappers and UStructSynchronizer gave me a deep \
appreciation for the gap between a powerful runtime and the tooling that lets \
developers trust it. I would bring the same instinct to Rive's SDK and documentation \
surface, turning edge-case discoveries into clear, reproducible examples.

I would welcome the chance to discuss how my background aligns with what your team \
is building. Thank you for your time.

Sincerely,
Andrei Kharlanchev
"""


class TestValidateClContent:
    def test_real_letter_passes(self):
        _validate_cl_content(_REAL_LETTER)  # must not raise

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            _validate_cl_content("Short note.")

    def test_summary_header_raises(self):
        # Long enough (>150 chars) to reach pattern check rather than length check
        bad = (
            "Cover letter drafted for Jane Doe — Software Engineer at Acme.\n\n"
            "Centerpiece: Strong Python background and five years of backend experience. "
            "Motivation framed around Acme's mission. Tone/length adjustments offered."
        )
        with pytest.raises(ValueError, match="meta-commentary"):
            _validate_cl_content(bad)

    def test_parenthetical_reference_raises(self):
        bad = (
            "(Cover letter delivered above in Russian. Awaiting any revision requests — "
            "tone, length, emphasis, or an English version. Happy to adjust on request. "
            "Let me know what changes you would like to see before we finalise.)"
        )
        with pytest.raises(ValueError, match="meta-commentary"):
            _validate_cl_content(bad)

    def test_delivered_above_raises(self):
        bad = (
            "The cover letter was delivered above. Here is a summary of the changes made "
            "during this session. The opening paragraph was rewritten to lead with "
            "motivation, and the achievements section was tightened to three bullet points."
        )
        with pytest.raises(ValueError, match="meta-commentary"):
            _validate_cl_content(bad)

    def test_awaiting_revision_raises(self):
        bad = (
            "Draft complete. Awaiting revision requests from the candidate before "
            "finalising. The current version addresses all points raised in the brief "
            "and mirrors the semi-formal tone specified in the style-capture step."
        )
        with pytest.raises(ValueError, match="meta-commentary"):
            _validate_cl_content(bad)

    def test_centerpiece_raises(self):
        bad = (
            "Centerpiece: Blueprint↔native-C++ tooling (Promise/Future, UStructSynchronizer).\n\n"
            "Advocacy framed honestly as instinct (open-source repos + doc/lead work), "
            "no fabricated tutorials/talks. Motivation reframed to point existing expertise "
            "at the runtime. Offered tone/length adjustments."
        )
        with pytest.raises(ValueError, match="meta-commentary"):
            _validate_cl_content(bad)
