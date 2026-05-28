"""Tests for Phase BF-6: POST /api/jobs/{job_id}/cancel route.

Covers:
- 404 when job not found
- 400 when job is not in running state (tests all non-running states)
- 200 when job is running: response state == "pending", current_stage == null
- DB state is actually pending after cancel (not just response)
- orchestrator.kick() is called
- StatusChangedEvent published with from_state="running", to_state="pending"
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import JobState
from jsa.events.bus import bus
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


# ---------------------------------------------------------------------------
# Fixtures (mirrors pattern in test_api.py exactly)
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
# Test: 404 when job not found
# ===========================================================================


class TestCancelNotFound:
    async def test_cancel_nonexistent_job_returns_404(self, client):
        """POST /cancel on a non-existent job_id must return 404."""
        resp = await client.post("/api/jobs/does-not-exist/cancel")
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]


# ===========================================================================
# Test: 400 when job is not running
# ===========================================================================


class TestCancelWrongState:
    async def test_cancel_pending_job_returns_400(self, client, db):
        await _insert_job(db, state=JobState.pending)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 400
        assert "expected 'running'" in resp.json()["detail"]

    async def test_cancel_failed_job_returns_400(self, client, db):
        await _insert_job(db, state=JobState.failed)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 400
        assert "expected 'running'" in resp.json()["detail"]

    async def test_cancel_review_job_returns_400(self, client, db):
        await _insert_job(db, state=JobState.review)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 400
        assert "expected 'running'" in resp.json()["detail"]

    async def test_cancel_approved_job_returns_400(self, client, db):
        await _insert_job(db, state=JobState.approved)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 400
        assert "expected 'running'" in resp.json()["detail"]

    async def test_cancel_dismissed_job_returns_400(self, client, db):
        await _insert_job(db, state=JobState.dismissed)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 400
        assert "expected 'running'" in resp.json()["detail"]


# ===========================================================================
# Test: 200 when job is running
# ===========================================================================


class TestCancelRunning:
    async def test_cancel_running_job_returns_200(self, client, db):
        """Cancelling a running job returns HTTP 200."""
        await _insert_job(db, state=JobState.running)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 200

    async def test_cancel_running_job_response_state_is_pending(self, client, db):
        """Response body has state == 'pending' after cancel."""
        await _insert_job(db, state=JobState.running)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "pending"

    async def test_cancel_running_job_response_current_stage_is_null(self, client, db):
        """Response body has current_stage == null after cancel."""
        await _insert_job(db, state=JobState.running)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_stage"] is None

    async def test_cancel_running_job_db_state_is_pending(self, client, db):
        """After cancel, the job row in DB is actually in pending state (not running)."""
        await _insert_job(db, state=JobState.running)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 200

        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
        assert job.state == JobState.pending
        assert job.current_stage is None


# ===========================================================================
# Test: orchestrator.kick() is called
# ===========================================================================


class TestCancelKicksOrchestrator:
    async def test_cancel_calls_orchestrator_kick(self, client, db, test_app, monkeypatch):
        """After cancel, orchestrator.kick() must have been called."""
        kick_calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: kick_calls.append(True))

        await _insert_job(db, state=JobState.running)
        resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
        assert resp.status_code == 200
        assert len(kick_calls) >= 1, "orchestrator.kick() was not called"


# ===========================================================================
# Test: StatusChangedEvent is published
# ===========================================================================


class TestCancelPublishesEvent:
    async def test_cancel_publishes_status_changed_event(self, client, db):
        """Cancel must publish a status_changed event with from_state=running, to_state=pending."""
        await _insert_job(db, state=JobState.running)

        q = bus.subscribe()
        try:
            resp = await client.post("/api/jobs/aabbccdd00112233/cancel")
            assert resp.status_code == 200

            event = q.get_nowait()
            assert event["type"] == "status_changed"
            assert event["job_id"] == "aabbccdd00112233"
            assert event["from_state"] == "running"
            assert event["to_state"] == "pending"
        finally:
            bus.unsubscribe(q)
