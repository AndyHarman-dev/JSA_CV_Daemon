"""Tests for Phase BF-12: DELETE /api/jobs/{job_id} hard-delete endpoint.

Covers:
- 200 + {"ok": True} when deleting an existing job
- 404 when job does not exist
- Job row is absent from DB after delete
- Cascade: Message, Document, FollowUp, and RevisionRequest rows are removed
- DELETE works for any job state (pending, failed, cv_done)
- JobRemovedEvent is published to the event bus with the correct job_id
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Document, FollowUp, Job, JobState, Message, RevisionRequest, Stage
from jsa.events.bus import bus
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


# ---------------------------------------------------------------------------
# Fixtures (mirrors pattern in test_cancel_bf6.py exactly)
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    """Create a FastAPI app with:
    - Orchestrator.run patched to a no-op (prevents background polling during tests)
    - backend_for patched to return FakeAgentBackend (no real agent calls)
    - tmp_path DB so the startup hook creates a fresh isolated SQLite file
    """
    from tests.backend.fakes.fake_backend import FakeAgentBackend

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
    """AsyncClient backed by ASGITransport with lifespan fired."""
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory after startup has run."""
    return test_app.state.session_factory


# ---------------------------------------------------------------------------
# Shared helpers
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


async def _insert_job(session_factory, *, state: JobState = JobState.pending, **overrides):
    """Insert a job via upsert_job and optionally override its state, then commit."""
    data = _job_data(**overrides)
    async with session_factory() as session:
        job = await repo.upsert_job(session, data)
        await session.commit()
        job_id = job.id
    if state != JobState.pending:
        async with session_factory() as session:
            job = await repo.get_job(session, job_id)
            job.state = state
            await session.commit()
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        return job


# ===========================================================================
# Test 1: DELETE existing job returns {"ok": True}
# ===========================================================================


class TestDeleteExistingJobReturnsOk:
    async def test_delete_existing_job_returns_200(self, client, db):
        """DELETE on an existing pending job returns HTTP 200."""
        await _insert_job(db, state=JobState.pending)
        resp = await client.delete("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200

    async def test_delete_existing_job_response_body_is_ok(self, client, db):
        """Response body is exactly {"ok": True}."""
        await _insert_job(db, state=JobState.pending)
        resp = await client.delete("/api/jobs/aabbccdd00112233")
        assert resp.json() == {"ok": True}


# ===========================================================================
# Test 2: DELETE nonexistent job returns 404
# ===========================================================================


class TestDeleteNonexistentJobReturns404:
    async def test_delete_nonexistent_job_returns_404(self, client):
        """DELETE on a non-existent job_id must return 404."""
        resp = await client.delete("/api/jobs/does-not-exist")
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]


# ===========================================================================
# Test 3: Job row is absent from DB after delete
# ===========================================================================


class TestDeleteRemovesJobFromDb:
    async def test_delete_removes_job_row(self, client, db):
        """After DELETE, the job row is gone from the DB."""
        await _insert_job(db, state=JobState.pending)

        resp = await client.delete("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200

        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
        assert job is None

    async def test_get_deleted_job_returns_404(self, client, db):
        """After DELETE, GET /api/jobs/{id} returns 404."""
        await _insert_job(db, state=JobState.pending)

        await client.delete("/api/jobs/aabbccdd00112233")
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 404

    async def test_deleted_job_absent_from_list(self, client, db):
        """After DELETE, the job no longer appears in GET /api/jobs."""
        await _insert_job(db, state=JobState.pending)

        await client.delete("/api/jobs/aabbccdd00112233")
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == []


# ===========================================================================
# Test 4: Cascade — child rows removed when job is deleted
# ===========================================================================


class TestDeleteCascadesChildRows:
    async def test_delete_cascades_messages_documents_followups_revisions(self, client, db):
        """DELETE job removes all child Message, Document, FollowUp, and
        RevisionRequest rows via ORM cascade."""
        job_id = "aabbccdd00112233"

        # Seed the job plus one row in every child table
        async with db() as session:
            job_data = _job_data(job_id=job_id)
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.cv_done

            msg = Message(
                job_id=job_id,
                stage=Stage.cv_adjust,
                role="user",
                content="Hello",
            )
            doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# CV",
            )
            fu = FollowUp(
                job_id=job_id,
                stage=Stage.cv_adjust,
                question="What is your target role?",
            )
            rev = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Make it shorter.",
            )
            session.add_all([msg, doc, fu, rev])
            await session.commit()

        # Verify all child rows exist before delete
        async with db() as session:
            msg_rows = (await session.execute(
                select(Message).where(Message.job_id == job_id)
            )).scalars().all()
            doc_rows = (await session.execute(
                select(Document).where(Document.job_id == job_id)
            )).scalars().all()
            fu_rows = (await session.execute(
                select(FollowUp).where(FollowUp.job_id == job_id)
            )).scalars().all()
            rev_rows = (await session.execute(
                select(RevisionRequest).where(RevisionRequest.job_id == job_id)
            )).scalars().all()

        assert len(msg_rows) == 1
        assert len(doc_rows) == 1
        assert len(fu_rows) == 1
        assert len(rev_rows) == 1

        # DELETE the job
        resp = await client.delete(f"/api/jobs/{job_id}")
        assert resp.status_code == 200

        # All child rows must be gone
        async with db() as session:
            msg_rows_after = (await session.execute(
                select(Message).where(Message.job_id == job_id)
            )).scalars().all()
            doc_rows_after = (await session.execute(
                select(Document).where(Document.job_id == job_id)
            )).scalars().all()
            fu_rows_after = (await session.execute(
                select(FollowUp).where(FollowUp.job_id == job_id)
            )).scalars().all()
            rev_rows_after = (await session.execute(
                select(RevisionRequest).where(RevisionRequest.job_id == job_id)
            )).scalars().all()

        assert msg_rows_after == []
        assert doc_rows_after == []
        assert fu_rows_after == []
        assert rev_rows_after == []


# ===========================================================================
# Test 5: DELETE works for any job state
# ===========================================================================


class TestDeleteAnyState:
    @pytest.mark.parametrize("state", [
        JobState.pending,
        JobState.failed,
        JobState.cv_done,
    ])
    async def test_delete_succeeds_for_state(self, client, db, state):
        """DELETE returns 200 regardless of job state."""
        await _insert_job(db, state=state)
        resp = await client.delete("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ===========================================================================
# Test 6: JobRemovedEvent published to the event bus
# ===========================================================================


class TestDeletePublishesJobRemovedEvent:
    async def test_delete_publishes_job_removed_event(self, client, db):
        """DELETE must publish a job_removed event with the correct job_id."""
        await _insert_job(db, state=JobState.pending)

        q = bus.subscribe()
        try:
            resp = await client.delete("/api/jobs/aabbccdd00112233")
            assert resp.status_code == 200

            event = q.get_nowait()
            assert event["type"] == "job_removed"
            assert event["job_id"] == "aabbccdd00112233"
        finally:
            bus.unsubscribe(q)

    async def test_delete_event_has_no_extra_state_fields(self, client, db):
        """The job_removed event is minimal: only type and job_id."""
        await _insert_job(db, state=JobState.pending)

        q = bus.subscribe()
        try:
            await client.delete("/api/jobs/aabbccdd00112233")
            event = q.get_nowait()
            # Must have exactly these two keys (from_state/to_state absent)
            assert "type" in event
            assert "job_id" in event
            assert "from_state" not in event
            assert "to_state" not in event
        finally:
            bus.unsubscribe(q)
