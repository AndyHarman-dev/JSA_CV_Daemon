"""Tests for BF-1 bugfixes:
1. State machine dismiss transitions
2. Dismiss API endpoint (POST /api/jobs/{id}/dismiss)
3. Reset endpoint accepts dismissed state
4. JD field present in API responses
5. Settings.agent_timeout defaults to 300.0
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jsa.db.models import JobState, Stage
from jsa.pipeline.state_machine import ALLOWED, InvalidTransition, transition


# ---------------------------------------------------------------------------
# Helpers (local copy — do not import from other test files)
# ---------------------------------------------------------------------------

def make_job(state: JobState, current_stage: Stage | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=state, current_stage=current_stage)


# ===========================================================================
# 1. State machine dismiss transitions
# ===========================================================================


class TestDismissTransitions:
    """pending/running/awaiting_input/cv_done/review/failed → dismissed succeeds.
    approved → dismissed and dismissed → failed raise InvalidTransition.
    dismissed → pending (undismiss) succeeds."""

    def test_pending_to_dismissed(self):
        job = make_job(JobState.pending)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        assert job.current_stage is None

    def test_running_to_dismissed(self):
        # running requires a current_stage to be in a valid state
        job = make_job(JobState.running, Stage.cv_adjust)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        # current_stage must be cleared (dismissed is not in STAGE_FOR_STATE)
        assert job.current_stage is None

    def test_awaiting_input_to_dismissed(self):
        job = make_job(JobState.awaiting_input, Stage.cv_adjust)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        assert job.current_stage is None

    def test_cv_done_to_dismissed(self):
        job = make_job(JobState.cv_done)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        assert job.current_stage is None

    def test_review_to_dismissed(self):
        job = make_job(JobState.review)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        assert job.current_stage is None

    def test_failed_to_dismissed(self):
        job = make_job(JobState.failed)
        transition(job, JobState.dismissed)
        assert job.state == JobState.dismissed
        assert job.current_stage is None

    def test_approved_to_dismissed_raises(self):
        """approved is terminal — any outgoing transition must raise."""
        job = make_job(JobState.approved)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.dismissed)

    def test_dismissed_to_pending(self):
        """Undismiss: dismissed → pending is the one allowed transition out."""
        job = make_job(JobState.dismissed)
        transition(job, JobState.pending)
        assert job.state == JobState.pending
        assert job.current_stage is None

    def test_dismissed_to_failed_raises(self):
        """dismissed → failed is NOT in ALLOWED[dismissed]."""
        job = make_job(JobState.dismissed)
        with pytest.raises(InvalidTransition):
            transition(job, JobState.failed)

    def test_dismissed_in_allowed_table(self):
        """Sanity check: dismissed key exists in ALLOWED and only has pending."""
        assert JobState.dismissed in ALLOWED
        assert ALLOWED[JobState.dismissed] == {JobState.pending}


# ===========================================================================
# API fixtures (mirror of test_api.py pattern)
# ===========================================================================

import pytest
from httpx import AsyncClient, ASGITransport

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Job
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    """FastAPI app with Orchestrator.run patched to noop, in-memory DB."""
    from tests.backend.fakes.fake_backend import FakeAgentBackend

    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name, **kwargs: FakeAgentBackend([]))

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
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory, available after lifespan has run."""
    return test_app.state.session_factory


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


async def _insert_job(
    session_factory, *, state: JobState = JobState.pending, **overrides
) -> Job:
    """Insert a job and optionally set its state."""
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
# 2. Dismiss API endpoint
# ===========================================================================


class TestDismissEndpoint:
    async def test_dismiss_pending_job_returns_200_and_dismissed_state(self, client, db):
        """Dismissing a pending job returns 200 and state='dismissed'."""
        await _insert_job(db, state=JobState.pending)
        resp = await client.post("/api/jobs/aabbccdd00112233/dismiss")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "dismissed"

    async def test_dismiss_approved_job_returns_400(self, client, db):
        """Dismissing an approved job returns 400."""
        await _insert_job(db, state=JobState.approved)
        resp = await client.post("/api/jobs/aabbccdd00112233/dismiss")
        assert resp.status_code == 400

    async def test_dismiss_already_dismissed_returns_400(self, client, db):
        """Dismissing an already-dismissed job returns 400."""
        await _insert_job(db, state=JobState.dismissed)
        resp = await client.post("/api/jobs/aabbccdd00112233/dismiss")
        assert resp.status_code == 400

    async def test_dismiss_nonexistent_job_returns_404(self, client):
        """Dismissing a non-existent job returns 404."""
        resp = await client.post("/api/jobs/doesnotexist00/dismiss")
        assert resp.status_code == 404


# ===========================================================================
# 3. Reset endpoint accepts dismissed state
# ===========================================================================


class TestResetDismissed:
    async def test_reset_dismissed_job_returns_200_and_pending(self, client, db):
        """POST /reset on a dismissed job should return 200 and state='pending'."""
        await _insert_job(db, state=JobState.dismissed)
        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "pending"

    async def test_reset_pending_returns_400(self, client, db):
        """POST /reset on a pending job should return 400 (unchanged from pre-BF-1)."""
        await _insert_job(db, state=JobState.pending)
        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 400


# ===========================================================================
# 4. JD field in API responses
# ===========================================================================


class TestJDInAPIResponse:
    async def test_list_jobs_includes_jd(self, client, db):
        """GET /api/jobs should include a 'jd' key on each job summary."""
        await _insert_job(db)
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) == 1
        assert "jd" in jobs[0]
        assert jobs[0]["jd"] == "Job description text"

    async def test_get_job_includes_jd(self, client, db):
        """GET /api/jobs/{id} should include a 'jd' key in the full job."""
        await _insert_job(db)
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        data = resp.json()
        assert "jd" in data
        assert data["jd"] == "Job description text"


# ===========================================================================
# 5. Settings.agent_timeout default
# ===========================================================================


class TestAgentTimeoutDefault:
    def test_agent_timeout_defaults_to_300(self, monkeypatch):
        """Settings().agent_timeout should default to 300.0."""
        monkeypatch.delenv("JSA_AGENT_TIMEOUT", raising=False)
        settings = Settings()
        assert settings.agent_timeout == 300.0
