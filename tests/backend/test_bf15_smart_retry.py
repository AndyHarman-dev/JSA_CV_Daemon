"""Tests for BF-15: Smart Retry — two-tier reset (soft vs nuclear).

Covers:
- mark_failed preserves current_stage
- soft_reset_job for cv_adjust failure (→ pending)
- soft_reset_job for cover_letter failure (→ cv_done, keeps cv_adjust messages)
- nuclear_reset_job deletes all data (→ pending, retry_count=0)
- POST /api/jobs/{id}/reset: soft on first retry (retry_count=0)
- POST /api/jobs/{id}/reset: nuclear on second retry (retry_count>0)
- _handle_final resets retry_count to 0 after a successful FINAL
- upsert_job on a failed job uses nuclear semantics
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from httpx import AsyncClient, ASGITransport

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
from jsa.pipeline.state_machine import transition


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


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    """Create a FastAPI test app with patched orchestrator (no background polling)."""
    from tests.backend.fakes.fake_backend import FakeAgentBackend
    from jsa.pipeline.orchestrator import Orchestrator
    from jsa.config import Settings
    from jsa.server import create_app

    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backend="claude-cli",
        port=8765,
        no_browser=True,
    )
    return create_app(settings)


@pytest.fixture
async def client(test_app):
    """AsyncClient backed by ASGITransport with lifespan context."""
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory for seeding data after startup has run."""
    return test_app.state.session_factory


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
    """Insert a job via upsert_job and commit.

    upsert_job creates fresh jobs as `queued` (parked pending LAUNCH); this
    fixture simulates an already-launched job so downstream transition()
    calls (running, etc.) match this file's expectations.
    """
    data = _job_data(**overrides)
    job = await repo.upsert_job(session, data)
    job.state = JobState.pending
    await session.commit()
    return job


async def _add_message(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
    role: str = "assistant",
    content: str = "some content",
) -> Message:
    """Directly insert a Message row and commit."""
    msg = Message(job_id=job_id, stage=stage, role=role, content=content)
    session.add(msg)
    await session.commit()
    return msg


# ---------------------------------------------------------------------------
# 1. mark_failed preserves current_stage
# ---------------------------------------------------------------------------


class TestMarkFailedPreservesStage:
    async def test_mark_failed_preserves_current_stage(self, session):
        """BF-15: mark_failed must leave current_stage = the failing stage."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        # Transition to running(cv_adjust)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "agent crashed")

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.state == JobState.failed
        assert refreshed.current_stage == Stage.cv_adjust  # preserved, not None

    async def test_mark_failed_preserves_none_stage(self, session):
        """mark_failed on a job that was pending (current_stage=None) keeps it None."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        # Job is pending; current_stage is None.  Simulate a pre-stage failure:
        # mark_failed is called from pending state (e.g., an init error).
        await repo.mark_failed(session, "aabbccdd00112233", "early init failure")

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.state == JobState.failed
        assert refreshed.current_stage is None  # was None, still None


# ---------------------------------------------------------------------------
# 3. soft_reset_job for cv_adjust failure
# ---------------------------------------------------------------------------


class TestSoftResetCvAdjustFailure:
    async def test_soft_reset_cv_adjust_state_becomes_pending(self, session):
        """Soft reset when cv_adjust failed → state=pending."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "timeout")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.state == JobState.pending

    async def test_soft_reset_cv_adjust_sets_retry_count_1(self, session):
        """Soft reset sets retry_count=1."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "timeout")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.retry_count == 1

    async def test_soft_reset_cv_adjust_clears_error_and_sessions(self, session):
        """Soft reset clears error, cv_session_id, session_external_id."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        job.cv_session_id = "old-cv-session"
        job.session_external_id = "old-ext-id"
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "timeout")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.error is None
        assert refreshed.cv_session_id is None
        assert refreshed.session_external_id is None

    async def test_soft_reset_cv_adjust_deletes_messages(self, session):
        """Soft reset for cv_adjust failure deletes all Message rows."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Seed cv_adjust messages
        await _add_message(session, job.id, Stage.cv_adjust, role="user", content="please fix")
        await _add_message(session, job.id, Stage.cv_adjust, role="assistant", content="here is")

        await repo.mark_failed(session, "aabbccdd00112233", "timeout")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        result = await session.execute(select(Message).where(Message.job_id == job.id))
        messages = list(result.scalars().all())
        assert len(messages) == 0


# ---------------------------------------------------------------------------
# 4. soft_reset_job for cover_letter failure
# ---------------------------------------------------------------------------


class TestSoftResetCoverLetterFailure:
    async def test_soft_reset_cover_letter_state_becomes_cv_done(self, session):
        """Soft reset when cover_letter failed → state=cv_done."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        # Simulate pipeline progress: cv_adjust completed, cover_letter running then failed
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "cl crashed")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.state == JobState.cv_done
        assert refreshed.retry_count == 1

    async def test_soft_reset_cover_letter_preserves_cv_adjust_messages(self, session):
        """Soft reset for cover_letter failure must NOT delete cv_adjust Messages."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        # Insert cv_adjust messages (should survive)
        await _add_message(session, job.id, Stage.cv_adjust, role="user", content="cv user msg")
        await _add_message(session, job.id, Stage.cv_adjust, role="assistant", content="cv asst msg")
        # Insert cover_letter messages (should be deleted)
        await _add_message(session, job.id, Stage.cover_letter, role="user", content="cl user msg")
        await _add_message(session, job.id, Stage.cover_letter, role="assistant", content="cl asst msg")

        await repo.mark_failed(session, "aabbccdd00112233", "cl crashed")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        # cv_adjust messages survive
        cv_msgs = await session.execute(
            select(Message).where(
                Message.job_id == job.id, Message.stage == Stage.cv_adjust
            )
        )
        assert len(list(cv_msgs.scalars().all())) == 2

        # cover_letter messages are deleted
        cl_msgs = await session.execute(
            select(Message).where(
                Message.job_id == job.id, Message.stage == Stage.cover_letter
            )
        )
        assert len(list(cl_msgs.scalars().all())) == 0

    async def test_soft_reset_cover_letter_clears_cl_session(self, session):
        """Soft reset for cover_letter failure clears cl_session_id and session_external_id,
        but preserves cv_session_id so cv_adjust can still be resumed."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        job.cv_session_id = "cv-sess"    # should survive
        job.cl_session_id = "cl-sess"    # should be cleared
        job.session_external_id = "ext"  # should be cleared
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "cl crashed")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.cl_session_id is None
        assert refreshed.session_external_id is None
        # Gap 1: cv_session_id must survive the cover_letter soft-reset so the
        # cv_adjust CLI session can be resumed for revisions.
        assert refreshed.cv_session_id == "cv-sess"

    async def test_soft_reset_cover_letter_deletes_unconsumed_revision_request(self, session):
        """Gap 2: soft_reset for cover_letter failure deletes unconsumed RevisionRequest rows."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        # Seed an unconsumed RevisionRequest
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cover_letter,
            instruction="make it shorter",
        )
        session.add(rr)
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "cl crashed")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        assert len(list(result.scalars().all())) == 0


# ---------------------------------------------------------------------------
# 4b. soft_reset_job for revision-stage failures (Gap 3)
# ---------------------------------------------------------------------------


class TestSoftResetRevisionStageFailure:
    async def test_soft_reset_revising_cv_becomes_pending(self, session):
        """Gap 3A: soft_reset when revising_cv failed → state=pending, all messages deleted."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        # Walk the job to running(revising_cv)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done, None)
        transition(job, JobState.review, None)
        transition(job, JobState.running, Stage.revising_cv)
        job.cv_session_id = "cv-sess-id"
        job.session_external_id = "ext-sess-id"
        await session.commit()

        # Seed some revising_cv messages
        await _add_message(session, job.id, Stage.revising_cv, role="user", content="please revise cv")
        await _add_message(session, job.id, Stage.revising_cv, role="assistant", content="revised")

        # Seed an unconsumed RevisionRequest
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="tighten the summary",
        )
        session.add(rr)
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "revising_cv crashed")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        # revising_cv is in the "else" branch → rewind to pending
        assert refreshed.state == JobState.pending
        assert refreshed.retry_count == 1
        # cv_session_id and session_external_id are cleared
        assert refreshed.cv_session_id is None
        assert refreshed.session_external_id is None

        # All Message rows deleted (else branch nukes all messages)
        msgs = await session.execute(select(Message).where(Message.job_id == job.id))
        assert len(list(msgs.scalars().all())) == 0

        # RevisionRequest deleted
        rrs = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        assert len(list(rrs.scalars().all())) == 0

    async def test_soft_reset_revising_cl_becomes_cv_done(self, session):
        """Gap 3B: soft_reset when revising_cl failed → state=cv_done, cv_adjust messages survive."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        # Walk the job to running(revising_cl)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done, None)
        transition(job, JobState.review, None)
        transition(job, JobState.running, Stage.revising_cl)
        job.cl_session_id = "cl-sess-id"
        job.session_external_id = "ext-sess-id"
        await session.commit()

        # Seed cv_adjust messages (should survive)
        await _add_message(session, job.id, Stage.cv_adjust, role="user", content="cv user msg")
        await _add_message(session, job.id, Stage.cv_adjust, role="assistant", content="cv asst msg")
        # Seed revising_cl messages (should be deleted)
        await _add_message(session, job.id, Stage.revising_cl, role="user", content="revise cl please")
        await _add_message(session, job.id, Stage.revising_cl, role="assistant", content="revised cl")

        # Seed an unconsumed RevisionRequest
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cover_letter,
            instruction="shorten it",
        )
        session.add(rr)
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "revising_cl crashed")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.soft_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        # revising_cl is in the cover_letter branch → rewind to cv_done
        assert refreshed.state == JobState.cv_done
        assert refreshed.retry_count == 1
        assert refreshed.cl_session_id is None
        assert refreshed.session_external_id is None

        # revising_cl messages deleted
        rcl_msgs = await session.execute(
            select(Message).where(
                Message.job_id == job.id, Message.stage == Stage.revising_cl
            )
        )
        assert len(list(rcl_msgs.scalars().all())) == 0

        # cv_adjust messages survive
        cv_msgs = await session.execute(
            select(Message).where(
                Message.job_id == job.id, Message.stage == Stage.cv_adjust
            )
        )
        assert len(list(cv_msgs.scalars().all())) == 2

        # RevisionRequest deleted
        rrs = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        assert len(list(rrs.scalars().all())) == 0


# ---------------------------------------------------------------------------
# 5. nuclear_reset_job deletes all data
# ---------------------------------------------------------------------------


class TestNuclearReset:
    async def test_nuclear_reset_state_becomes_pending(self, session):
        """nuclear_reset_job → state=pending."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "crash")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.nuclear_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.state == JobState.pending
        assert refreshed.retry_count == 0

    async def test_nuclear_reset_deletes_messages(self, session):
        """nuclear_reset_job deletes all Message rows."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        await _add_message(session, job.id, Stage.cv_adjust, role="user")
        await _add_message(session, job.id, Stage.cover_letter, role="assistant")

        await repo.mark_failed(session, "aabbccdd00112233", "crash")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.nuclear_reset_job(session, job)

        result = await session.execute(select(Message).where(Message.job_id == job.id))
        assert len(list(result.scalars().all())) == 0

    async def test_nuclear_reset_deletes_documents(self, session):
        """nuclear_reset_job deletes all Document rows."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        await session.commit()

        doc = Document(job_id=job.id, stage=Stage.cv_adjust, version=1, markdown="# CV")
        session.add(doc)
        await session.commit()

        # Transition to failed directly for test purposes
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()
        await repo.mark_failed(session, "aabbccdd00112233", "crash")

        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.nuclear_reset_job(session, job)

        result = await session.execute(select(Document).where(Document.job_id == job.id))
        assert len(list(result.scalars().all())) == 0

    async def test_nuclear_reset_deletes_followups(self, session):
        """nuclear_reset_job deletes all FollowUp rows."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        fu = FollowUp(job_id=job.id, stage=Stage.cv_adjust, question="What role?")
        session.add(fu)
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "crash")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.nuclear_reset_job(session, job)

        result = await session.execute(select(FollowUp).where(FollowUp.job_id == job.id))
        assert len(list(result.scalars().all())) == 0

    async def test_nuclear_reset_clears_session_ids(self, session):
        """nuclear_reset_job clears all session ID fields."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        job.cv_session_id = "cv-sess"
        job.cl_session_id = "cl-sess"
        job.session_external_id = "ext-sess"
        await session.commit()

        await repo.mark_failed(session, "aabbccdd00112233", "crash")
        job = await repo.get_job(session, "aabbccdd00112233")
        await repo.nuclear_reset_job(session, job)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.cv_session_id is None
        assert refreshed.cl_session_id is None
        assert refreshed.session_external_id is None
        assert refreshed.error is None


# ---------------------------------------------------------------------------
# 6. POST /api/jobs/{id}/reset — soft on first retry
# ---------------------------------------------------------------------------


class TestResetEndpointSoft:
    async def test_reset_soft_on_first_retry(self, client, db):
        """POST /reset on a failed job with retry_count=0 → soft reset → state=pending."""
        async with db() as session:
            job = await repo.upsert_job(session, _job_data(job_id="aabbccdd00112233"))
            await session.commit()
            # Set to failed/cv_adjust with retry_count=0
            job.state = JobState.failed
            job.current_stage = Stage.cv_adjust
            job.error = "some error"
            job.retry_count = 0
            await session.commit()

        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200
        body = resp.json()
        # cv_adjust soft reset → pending
        assert body["state"] == "pending"
        assert body["retry_count"] == 1

    async def test_reset_soft_cover_letter_goes_to_cv_done(self, client, db):
        """Soft reset when cover_letter failed → state=cv_done."""
        async with db() as session:
            job = await repo.upsert_job(session, _job_data(job_id="aabbccdd00112233"))
            await session.commit()
            job.state = JobState.failed
            job.current_stage = Stage.cover_letter
            job.error = "cl error"
            job.retry_count = 0
            await session.commit()

        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "cv_done"
        assert body["retry_count"] == 1


# ---------------------------------------------------------------------------
# 7. POST /api/jobs/{id}/reset — nuclear on second retry
# ---------------------------------------------------------------------------


class TestResetEndpointNuclear:
    async def test_reset_nuclear_on_second_retry(self, client, db):
        """POST /reset on a failed job with retry_count=1 → nuclear → state=pending, retry_count=0."""
        async with db() as session:
            job = await repo.upsert_job(session, _job_data(job_id="aabbccdd00112233"))
            await session.commit()
            job.state = JobState.failed
            job.current_stage = Stage.cv_adjust
            job.error = "some error"
            job.retry_count = 1
            await session.commit()

        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "pending"
        assert body["retry_count"] == 0

    async def test_reset_nuclear_deletes_messages(self, client, db):
        """Nuclear reset via /reset endpoint clears all Messages."""
        async with db() as session:
            job = await repo.upsert_job(session, _job_data(job_id="aabbccdd00112233"))
            await session.commit()
            # Add a Message row
            msg = Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="hello")
            session.add(msg)
            job.state = JobState.failed
            job.current_stage = Stage.cv_adjust
            job.error = "crash"
            job.retry_count = 1
            await session.commit()

        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200

        async with db() as session:
            result = await session.execute(
                select(Message).where(Message.job_id == "aabbccdd00112233")
            )
            assert len(list(result.scalars().all())) == 0


# ---------------------------------------------------------------------------
# 8. _handle_final resets retry_count to 0
# ---------------------------------------------------------------------------


class TestRetryCountResetOnFinal:
    async def test_retry_count_resets_on_successful_final(self, session):
        """After run_stage completes with a FINAL reply, retry_count is reset to 0."""
        from jsa.agents.base import AgentReply
        from jsa.pipeline.stages import run_stage
        from tests.backend.fakes.fake_backend import FakeAgentBackend

        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        # Simulate a prior soft retry
        job.retry_count = 1
        await session.commit()

        final_reply = AgentReply(
            raw="<<<FINAL>>>\n# Adjusted CV\n<<<END>>>",
            content="# Adjusted CV",
            kind="final",
        )
        backend = FakeAgentBackend([final_reply])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, "aabbccdd00112233")
        assert refreshed.retry_count == 0


# ---------------------------------------------------------------------------
# 9. upsert_job on a failed job is nuclear
# ---------------------------------------------------------------------------


class TestUpsertJobNuclearForFailed:
    async def test_upsert_failed_job_clears_messages_and_resets(self, session):
        """upsert_job on a failed job uses nuclear semantics: Messages deleted, state=pending."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Add messages
        await _add_message(session, job.id, Stage.cv_adjust, role="user")

        # Fail the job
        await repo.mark_failed(session, "aabbccdd00112233", "original failure")

        # Verify the messages exist before upsert
        pre_result = await session.execute(
            select(Message).where(Message.job_id == "aabbccdd00112233")
        )
        assert len(list(pre_result.scalars().all())) == 1

        # Re-import the job (upsert with new data — simulates CSV re-run)
        new_data = _job_data(
            job_id="aabbccdd00112233", jd="new JD", jd_hash="newhash0000bbbb"
        )
        updated_job = await repo.upsert_job(session, new_data)

        assert updated_job.state == JobState.pending
        assert updated_job.retry_count == 0

        post_result = await session.execute(
            select(Message).where(Message.job_id == "aabbccdd00112233")
        )
        assert len(list(post_result.scalars().all())) == 0

    async def test_upsert_failed_job_clears_error(self, session):
        """upsert_job on a failed job clears error."""
        job = await _insert_job(session, job_id="aabbccdd00112233")
        await repo.mark_failed(session, "aabbccdd00112233", "some failure")

        new_data = _job_data(job_id="aabbccdd00112233")
        updated_job = await repo.upsert_job(session, new_data)

        assert updated_job.error is None
