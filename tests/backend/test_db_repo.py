"""Tests for jsa.db.repo: repository functions against in-memory SQLite."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.db.models import Base, Document, FollowUp, Job, JobState, RevisionRequest, Stage
from jsa.db import repo
from jsa.pipeline.state_machine import InvalidTransition


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def session_factory():
    """Create an in-memory SQLite engine with StaticPool (shared connection).

    StaticPool is required so multiple sessions see the same in-memory DB.
    """
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
    """Yield a single AsyncSession for use in most tests."""
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


# ---------------------------------------------------------------------------
# upsert_job tests
# ---------------------------------------------------------------------------

class TestUpsertJob:
    async def test_insert_new_job_has_pending_state(self, session):
        job = await _insert_job(session)
        assert job.state == JobState.pending
        assert job.current_stage is None
        assert job.error is None

    async def test_insert_new_job_fields_stored_correctly(self, session):
        job = await _insert_job(session, company="Beta Corp", role="Designer", tier="B")
        assert job.company == "Beta Corp"
        assert job.role == "Designer"
        assert job.tier == "B"

    async def test_upsert_existing_updates_jd_and_hash(self, session):
        await _insert_job(session, jd="old JD", jd_hash="oldhash0000aaaa")
        # Now update it
        data = _job_data(jd="new JD", jd_hash="newhash0000bbbb")
        job = await repo.upsert_job(session, data)
        await session.commit()
        assert job.jd == "new JD"
        assert job.jd_hash == "newhash0000bbbb"

    async def test_upsert_existing_updates_tier(self, session):
        await _insert_job(session, tier="A")
        data = _job_data(tier="C")
        job = await repo.upsert_job(session, data)
        await session.commit()
        assert job.tier == "C"

    async def test_upsert_existing_does_not_overwrite_state(self, session):
        job = await _insert_job(session)
        # Manually change state to cv_done (simulate pipeline progress)
        job.state = JobState.cv_done
        job.current_stage = None
        await session.commit()
        # Upsert again — state should NOT be reset to pending
        data = _job_data(jd="updated JD", jd_hash="updatedhashaaaa")
        updated_job = await repo.upsert_job(session, data)
        await session.commit()
        assert updated_job.state == JobState.cv_done

    async def test_upsert_existing_does_not_overwrite_current_stage(self, session):
        job = await _insert_job(session)
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()
        data = _job_data(jd="updated JD", jd_hash="updatedhashbbbb")
        updated_job = await repo.upsert_job(session, data)
        await session.commit()
        assert updated_job.current_stage == Stage.cv_adjust

    async def test_upsert_failed_job_resets_to_pending(self, session):
        """Per ARCH.md § Job identity: failed jobs are reset to pending on re-run."""
        job = await _insert_job(session)
        job.state = JobState.failed
        job.error = "something went wrong"
        await session.commit()
        data = _job_data(jd="updated JD", jd_hash="updatedhashcccc")
        updated_job = await repo.upsert_job(session, data)
        await session.commit()
        assert updated_job.state == JobState.pending
        assert updated_job.error is None

    async def test_upsert_returns_same_job_object_on_update(self, session):
        job1 = await _insert_job(session)
        data = _job_data(jd="changed", jd_hash="changeddddddddd")
        job2 = await repo.upsert_job(session, data)
        await session.commit()
        # Same primary key
        assert job1.id == job2.id


# ---------------------------------------------------------------------------
# get_job tests
# ---------------------------------------------------------------------------

class TestGetJob:
    async def test_get_job_returns_none_for_missing_id(self, session):
        result = await repo.get_job(session, "nonexistentid00")
        assert result is None

    async def test_get_job_returns_job_for_known_id(self, session):
        await _insert_job(session)
        result = await repo.get_job(session, "aabbccdd00112233")
        assert result is not None
        assert result.id == "aabbccdd00112233"
        assert result.company == "Acme"


# ---------------------------------------------------------------------------
# list_jobs tests
# ---------------------------------------------------------------------------

class TestListJobs:
    async def test_list_jobs_no_filter_returns_all(self, session):
        await _insert_job(session, job_id="aaaa000000000001", company="A")
        await _insert_job(session, job_id="aaaa000000000002", company="B")
        await _insert_job(session, job_id="aaaa000000000003", company="C")
        jobs = await repo.list_jobs(session)
        assert len(jobs) == 3

    async def test_list_jobs_state_filter_returns_subset(self, session):
        job1 = await _insert_job(session, job_id="aaaa000000000001")
        job2 = await _insert_job(session, job_id="aaaa000000000002")
        # Move job2 to cv_done
        job2.state = JobState.cv_done
        await session.commit()

        pending_jobs = await repo.list_jobs(session, state=JobState.pending)
        assert len(pending_jobs) == 1
        assert pending_jobs[0].id == job1.id

        cv_done_jobs = await repo.list_jobs(session, state=JobState.cv_done)
        assert len(cv_done_jobs) == 1
        assert cv_done_jobs[0].id == job2.id

    async def test_list_jobs_filter_returns_empty_when_no_match(self, session):
        await _insert_job(session, job_id="aaaa000000000001")
        result = await repo.list_jobs(session, state=JobState.approved)
        assert result == []

    async def test_list_jobs_empty_db_returns_empty(self, session):
        result = await repo.list_jobs(session)
        assert result == []


# ---------------------------------------------------------------------------
# list_runnable_jobs tests
# ---------------------------------------------------------------------------

class TestListRunnableJobs:
    async def test_pending_job_is_returned(self, session):
        await _insert_job(session, job_id="aaaa000000000001")
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 1
        assert runnable[0].state == JobState.pending

    async def test_cv_done_job_is_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.cv_done
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 1
        assert runnable[0].state == JobState.cv_done

    async def test_awaiting_input_with_unanswered_followup_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()
        # Insert an unanswered FollowUp
        fu = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="What is your availability?",
            answer=None,
            answered_at=None,
        )
        session.add(fu)
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_awaiting_input_with_answered_followup_is_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()
        # Insert an answered FollowUp
        fu = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="What is your availability?",
            answer="Full-time",
            answered_at=datetime.utcnow(),
        )
        session.add(fu)
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 1
        assert runnable[0].id == job.id

    async def test_review_with_no_revision_request_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_review_with_unconsumed_revision_is_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 1
        assert runnable[0].id == job.id

    async def test_review_with_consumed_revision_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=datetime.utcnow(),
        )
        session.add(rr)
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_results_ordered_fifo_by_updated_at(self, session):
        """Jobs should be ordered ascending by updated_at (oldest first)."""
        # Insert jobs with distinct updated_at by slightly changing the timestamp
        job1 = await _insert_job(session, job_id="aaaa000000000001")
        # Small sleep to ensure updated_at differs
        await asyncio.sleep(0.01)
        job2 = await _insert_job(session, job_id="aaaa000000000002")
        await asyncio.sleep(0.01)
        job3 = await _insert_job(session, job_id="aaaa000000000003")

        runnable = await repo.list_runnable_jobs(session)
        ids = [j.id for j in runnable]
        assert ids == [job1.id, job2.id, job3.id]

    async def test_running_job_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_failed_job_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.failed
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_approved_job_not_returned(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.approved
        await session.commit()
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0

    async def test_awaiting_input_with_answered_and_open_followup_not_returned(
        self, session
    ):
        """BF-8 Fix 1: a job with one answered AND one still-open FollowUp must NOT
        be returned as runnable — the open follow-up holds it back."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()

        # FollowUp1: answered (answered_at IS NOT NULL — excluded from partial unique index)
        fu1 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="First question",
            answer="First answer",
            answered_at=datetime.utcnow(),
        )
        session.add(fu1)
        await session.commit()

        # FollowUp2: open (answered_at IS NULL — covered by partial unique index).
        # Inserting is legal because fu1 is NOT open, so only one open row exists.
        fu2 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="Second question",
            answer=None,
            answered_at=None,
        )
        session.add(fu2)
        await session.commit()

        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0, (
            "Job should NOT be runnable while an open FollowUp remains"
        )

    async def test_awaiting_input_becomes_runnable_only_after_all_followups_answered(
        self, session
    ):
        """BF-8 Fix 1: the job becomes runnable only once every FollowUp is answered."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()

        # Insert answered FollowUp1
        fu1 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="First question",
            answer="First answer",
            answered_at=datetime.utcnow(),
        )
        session.add(fu1)
        await session.commit()

        # Insert open FollowUp2
        fu2 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="Second question",
            answer=None,
            answered_at=None,
        )
        session.add(fu2)
        await session.commit()

        # Confirm not runnable while fu2 is still open
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 0, "Job must not be runnable before fu2 is answered"

        # User answers fu2
        fu2.answer = "Second answer"
        fu2.answered_at = datetime.utcnow()
        await session.commit()

        # Now both FollowUps are answered — job should become runnable
        runnable = await repo.list_runnable_jobs(session)
        assert len(runnable) == 1, "Job must be runnable once all FollowUps are answered"
        assert runnable[0].id == job.id


# ---------------------------------------------------------------------------
# mark_failed tests
# ---------------------------------------------------------------------------

class TestMarkFailed:
    async def test_mark_failed_sets_state_and_error(self, session):
        await _insert_job(session, job_id="aaaa000000000001")
        await repo.mark_failed(session, "aaaa000000000001", "timeout error")
        job = await repo.get_job(session, "aaaa000000000001")
        assert job.state == JobState.failed
        assert job.error == "timeout error"

    async def test_mark_failed_preserves_current_stage(self, session):
        """BF-15: mark_failed preserves current_stage so soft_reset_job can discriminate.

        The old behavior (clear current_stage) was intentionally changed in BF-15.
        Failed jobs retain current_stage = the stage that failed; they are never
        dispatched by list_runnable_jobs, so this is safe.
        """
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()
        await repo.mark_failed(session, "aaaa000000000001", "agent crashed")
        refreshed = await repo.get_job(session, "aaaa000000000001")
        assert refreshed.state == JobState.failed
        assert refreshed.current_stage == Stage.cv_adjust  # preserved, not cleared

    async def test_mark_failed_noop_for_missing_job(self, session):
        # Should not raise
        await repo.mark_failed(session, "doesnotexist0000", "error")

    async def test_after_mark_failed_transition_to_pending_works(self, session):
        """After mark_failed, the job is in 'failed' state; transition to pending is allowed."""
        await _insert_job(session, job_id="aaaa000000000001")
        await repo.mark_failed(session, "aaaa000000000001", "some error")
        job = await repo.get_job(session, "aaaa000000000001")
        assert job.state == JobState.failed
        # Now transition to pending (as reset would do)
        from jsa.pipeline.state_machine import transition
        transition(job, JobState.pending)
        assert job.state == JobState.pending


# ---------------------------------------------------------------------------
# checkpoint tests
# ---------------------------------------------------------------------------

class TestCheckpoint:
    async def test_happy_path_updates_job_state_and_stage(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        await repo.checkpoint(
            session,
            job,
            new_state=JobState.cv_done,
            new_stage=None,
        )
        refreshed = await repo.get_job(session, "aaaa000000000001")
        assert refreshed.state == JobState.cv_done
        assert refreshed.current_stage is None

    async def test_checkpoint_inserts_message_rows(self, session):
        from sqlalchemy import select
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        messages = [
            {"role": "user", "content": "Please adjust my CV"},
            {"role": "assistant", "content": "Here is the adjusted CV"},
        ]
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.cv_done,
            new_stage=None,
            messages=messages,
        )
        from jsa.db.models import Message
        result = await session.execute(
            select(Message).where(Message.job_id == "aaaa000000000001")
        )
        msgs = list(result.scalars().all())
        assert len(msgs) == 2
        roles = {m.role for m in msgs}
        assert roles == {"user", "assistant"}

    async def test_checkpoint_inserts_document_row(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        doc = {"stage": Stage.cv_adjust, "version": 1, "markdown": "# Adjusted CV"}
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.cv_done,
            new_stage=None,
            document=doc,
        )
        docs = await repo.get_documents(session, "aaaa000000000001")
        assert len(docs) == 1
        assert docs[0].markdown == "# Adjusted CV"
        assert docs[0].version == 1

    async def test_checkpoint_inserts_followup(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        follow_up = {"stage": Stage.cv_adjust, "question": "What is your target role?"}
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.awaiting_input,
            new_stage=Stage.cv_adjust,
            follow_up=follow_up,
        )
        fus = await repo.get_follow_ups(session, "aaaa000000000001")
        assert len(fus) == 1
        assert fus[0].question == "What is your target role?"
        assert fus[0].answered_at is None

    async def test_checkpoint_answers_followup(self, session):
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        # First checkpoint: insert the follow-up, park the job
        follow_up_insert = {"stage": Stage.cv_adjust, "question": "What is your target role?"}
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.awaiting_input,
            new_stage=Stage.cv_adjust,
            follow_up=follow_up_insert,
        )
        fus = await repo.get_follow_ups(session, "aaaa000000000001")
        fu_id = fus[0].id

        # User answers — second checkpoint: mark the follow-up answered
        answered_at = datetime.utcnow()
        follow_up_answer = {
            "follow_up_id": fu_id,
            "answer": "Software Engineer",
            "answered_at": answered_at,
        }
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.running,
            new_stage=Stage.cv_adjust,
            follow_up=follow_up_answer,
        )
        fus_after = await repo.get_follow_ups(session, "aaaa000000000001", answered=True)
        assert len(fus_after) == 1
        assert fus_after[0].answer == "Software Engineer"
        assert fus_after[0].answered_at is not None

    async def test_checkpoint_invalid_transition_raises_and_does_not_commit(
        self, session_factory
    ):
        """If transition() raises InvalidTransition, the DB must be unchanged."""
        async with session_factory() as s1:
            job = await _insert_job(s1, job_id="aaaa000000000001")
            # job is currently pending

        # Now in a fresh session, attempt a bad checkpoint (pending → approved is invalid)
        async with session_factory() as s2:
            job2 = await repo.get_job(s2, "aaaa000000000001")
            with pytest.raises(InvalidTransition):
                await repo.checkpoint(
                    s2,
                    job2,
                    new_state=JobState.approved,  # pending → approved is NOT allowed
                    new_stage=None,
                )

        # Verify job still in pending state in a third session
        async with session_factory() as s3:
            job3 = await repo.get_job(s3, "aaaa000000000001")
            assert job3.state == JobState.pending

    async def test_checkpoint_with_no_messages_works(self, session):
        """Calling checkpoint without messages list still works (defaults to empty list)."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        await repo.checkpoint(
            session,
            job,
            new_state=JobState.cv_done,
            new_stage=None,
            # messages not passed
        )
        assert job.state == JobState.cv_done

    async def test_checkpoint_replaces_stale_open_followup(self, session):
        """BF-8 Fix 2: if an open FollowUp already exists for (job_id, stage), checkpoint
        must delete it before inserting the new one — no IntegrityError should be raised."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.running
        job.current_stage = Stage.cv_adjust
        await session.commit()

        # Simulate a stale open FollowUp left by a previous failed run
        stale_fu = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="old question",
            answer=None,
            answered_at=None,
        )
        session.add(stale_fu)
        await session.commit()

        # This checkpoint should succeed without raising IntegrityError
        await repo.checkpoint(
            session,
            job,
            new_state=JobState.awaiting_input,
            new_stage=Stage.cv_adjust,
            follow_up={"stage": Stage.cv_adjust, "question": "new question"},
        )

        # There must be exactly ONE open FollowUp for this job (the new one)
        open_fus = await repo.get_follow_ups(session, job.id, answered=False)
        assert len(open_fus) == 1, "Expected exactly one open FollowUp after checkpoint"
        assert open_fus[0].question == "new question", (
            "The stale FollowUp should have been replaced by the new one"
        )

        # The total FollowUp count must also be 1 (stale row was deleted, not kept)
        all_fus = await repo.get_follow_ups(session, job.id)
        assert len(all_fus) == 1, "Stale FollowUp must be deleted, not merely shadowed"


# ---------------------------------------------------------------------------
# get_documents tests
# ---------------------------------------------------------------------------

class TestGetDocuments:
    async def _setup_job_with_docs(self, session: AsyncSession) -> str:
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()
        # Insert three documents: 2 cv_adjust versions, 1 cover_letter
        docs = [
            Document(job_id=job.id, stage=Stage.cv_adjust, version=1, markdown="CV v1"),
            Document(job_id=job.id, stage=Stage.cv_adjust, version=2, markdown="CV v2"),
            Document(job_id=job.id, stage=Stage.cover_letter, version=1, markdown="CL v1"),
        ]
        for d in docs:
            session.add(d)
        await session.commit()
        return job.id

    async def test_get_documents_all_for_job(self, session):
        job_id = await self._setup_job_with_docs(session)
        docs = await repo.get_documents(session, job_id)
        assert len(docs) == 3

    async def test_get_documents_filter_by_stage(self, session):
        job_id = await self._setup_job_with_docs(session)
        cv_docs = await repo.get_documents(session, job_id, stage=Stage.cv_adjust)
        assert len(cv_docs) == 2
        assert all(d.stage == Stage.cv_adjust for d in cv_docs)

    async def test_get_documents_ordered_by_version_desc(self, session):
        job_id = await self._setup_job_with_docs(session)
        cv_docs = await repo.get_documents(session, job_id, stage=Stage.cv_adjust)
        versions = [d.version for d in cv_docs]
        assert versions == [2, 1]  # descending

    async def test_get_documents_returns_empty_for_missing_job(self, session):
        docs = await repo.get_documents(session, "nonexistentid00")
        assert docs == []

    async def test_get_documents_no_filter_includes_all_stages(self, session):
        job_id = await self._setup_job_with_docs(session)
        docs = await repo.get_documents(session, job_id)
        stages = {d.stage for d in docs}
        assert Stage.cv_adjust in stages
        assert Stage.cover_letter in stages


# ---------------------------------------------------------------------------
# get_follow_ups tests
# ---------------------------------------------------------------------------

class TestGetFollowUps:
    async def _setup_job_with_followups(self, session: AsyncSession) -> str:
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()
        # Insert one answered and one unanswered follow-up
        fu_answered = FollowUp(
            job_id=job.id,
            stage=Stage.cover_letter,
            question="What industry?",
            answer="Tech",
            answered_at=datetime.utcnow(),
        )
        fu_open = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="Target role?",
            answer=None,
            answered_at=None,
        )
        session.add(fu_answered)
        session.add(fu_open)
        await session.commit()
        return job.id

    async def test_get_follow_ups_all(self, session):
        job_id = await self._setup_job_with_followups(session)
        fus = await repo.get_follow_ups(session, job_id)
        assert len(fus) == 2

    async def test_get_follow_ups_answered_false_returns_unanswered(self, session):
        job_id = await self._setup_job_with_followups(session)
        fus = await repo.get_follow_ups(session, job_id, answered=False)
        assert len(fus) == 1
        assert fus[0].answered_at is None
        assert fus[0].answer is None

    async def test_get_follow_ups_answered_true_returns_answered(self, session):
        job_id = await self._setup_job_with_followups(session)
        fus = await repo.get_follow_ups(session, job_id, answered=True)
        assert len(fus) == 1
        assert fus[0].answered_at is not None
        assert fus[0].answer == "Tech"

    async def test_get_follow_ups_empty_for_missing_job(self, session):
        fus = await repo.get_follow_ups(session, "nonexistentid00")
        assert fus == []


# ---------------------------------------------------------------------------
# Partial unique index tests
# ---------------------------------------------------------------------------

class TestPartialUniqueIndexes:
    async def test_two_open_followups_same_job_stage_raises_integrity_error(self, session):
        """Inserting two open (unanswered) FollowUps for the same (job, stage) must raise."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()

        fu1 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="First question",
            answer=None,
            answered_at=None,
        )
        session.add(fu1)
        await session.commit()

        # Second open FollowUp for same (job, stage) — must violate partial unique index
        fu2 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="Second question",
            answer=None,
            answered_at=None,
        )
        session.add(fu2)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_two_open_followups_different_stages_are_allowed(self, session):
        """Two open FollowUps for different stages should NOT violate the constraint."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.awaiting_input
        job.current_stage = Stage.cv_adjust
        await session.commit()

        fu1 = FollowUp(
            job_id=job.id,
            stage=Stage.cv_adjust,
            question="CV question",
            answer=None,
            answered_at=None,
        )
        session.add(fu1)
        await session.commit()

        fu2 = FollowUp(
            job_id=job.id,
            stage=Stage.cover_letter,
            question="CL question",
            answer=None,
            answered_at=None,
        )
        session.add(fu2)
        # Should not raise
        await session.commit()
        fus = await repo.get_follow_ups(session, job.id)
        assert len(fus) == 2

    async def test_two_unconsumed_revision_requests_same_job_raises_integrity_error(
        self, session
    ):
        """Two unconsumed RevisionRequests for the same job must raise IntegrityError."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()

        rr1 = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr1)
        await session.commit()

        # Second unconsumed RevisionRequest — violates partial unique index
        rr2 = RevisionRequest(
            job_id=job.id,
            target=Stage.cover_letter,
            instruction="Make it longer",
            consumed_at=None,
        )
        session.add(rr2)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_consumed_then_unconsumed_revision_allowed(self, session):
        """A consumed RevisionRequest followed by a new unconsumed one is allowed."""
        job = await _insert_job(session, job_id="aaaa000000000001")
        job.state = JobState.review
        await session.commit()

        rr1 = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="First revision",
            consumed_at=datetime.utcnow(),  # already consumed
        )
        session.add(rr1)
        await session.commit()

        rr2 = RevisionRequest(
            job_id=job.id,
            target=Stage.cover_letter,
            instruction="Second revision",
            consumed_at=None,  # new open revision
        )
        session.add(rr2)
        # Should not raise — first is consumed, only one unconsumed
        await session.commit()
