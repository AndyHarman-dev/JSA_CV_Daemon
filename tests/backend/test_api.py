"""Tests for Phase 8: FastAPI backend — EventBus, event schema, and HTTP routes.

Covers:
- EventBus pub/sub behaviour (pure unit tests, no HTTP)
- Event schema dataclasses (event_to_dict)
- Meta routes: /api/health, /api/config
- Job routes: list, get, answer, approve, revise, reset, document
- Recovery sweep (direct DB tests, no HTTP)
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Document, FollowUp, Job, JobState, Stage
from jsa.events.bus import EventBus
from jsa.events.schema import (
    ApprovedEvent,
    StatusChangedEvent,
    event_to_dict,
)
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
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
    """AsyncClient backed by ASGITransport.

    ASGITransport does not automatically fire FastAPI's @on_event("startup")
    handlers, so we trigger the lifespan context manually before yielding the
    client, then shut it down afterwards.
    """
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory for seeding data after startup has run.

    Depends on `client` so the lifespan/startup has already populated
    app.state.session_factory before this fixture is used.
    """
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


async def _insert_job(session_factory, *, state: JobState = JobState.pending, **overrides) -> Job:
    """Insert a job via upsert_job and optionally override its state, then commit."""
    data = _job_data(**overrides)
    async with session_factory() as session:
        job = await repo.upsert_job(session, data)
        await session.commit()
        job_id = job.id
    # Re-open session to set state — upsert_job always creates fresh jobs as
    # `queued` (parked pending LAUNCH), so always force the fixture's state to
    # the requested one (default `pending`, the already-launched baseline most
    # tests expect).
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        job.state = state
        await session.commit()
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        return job


# ===========================================================================
# EventBus unit tests (no HTTP)
# ===========================================================================


class TestEventBus:
    async def test_publish_reaches_subscriber(self):
        bus = EventBus()
        q = bus.subscribe()
        event = {"type": "status_changed", "job_id": "abc"}
        await bus.publish(event)
        received = q.get_nowait()
        assert received == event

    async def test_publish_reaches_multiple_subscribers(self):
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        event = {"type": "log", "job_id": "xyz"}
        await bus.publish(event)
        assert q1.get_nowait() == event
        assert q2.get_nowait() == event

    async def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        await bus.publish({"type": "log", "job_id": "xyz"})
        # Queue should be empty
        with pytest.raises(asyncio.QueueEmpty):
            q.get_nowait()

    async def test_publish_does_not_crash_with_no_subscribers(self):
        bus = EventBus()
        # Should not raise
        await bus.publish({"type": "log", "job_id": "xyz"})


# ===========================================================================
# Event schema tests (no HTTP)
# ===========================================================================


class TestEventSchema:
    def test_event_to_dict_has_type_key(self):
        event = StatusChangedEvent(job_id="abc", from_state="pending", to_state="running")
        d = event_to_dict(event)
        assert d["type"] == "status_changed"
        assert d["job_id"] == "abc"
        assert d["from_state"] == "pending"
        assert d["to_state"] == "running"

    def test_approved_event_to_dict(self):
        event = ApprovedEvent(
            job_id="abc",
            cv_pdf_path="/tmp/cv.pdf",
            cl_pdf_path="/tmp/cl.pdf",
        )
        d = event_to_dict(event)
        assert d["type"] == "approved"
        assert "cv_pdf_path" in d
        assert "cl_pdf_path" in d
        assert d["cv_pdf_path"] == "/tmp/cv.pdf"
        assert d["cl_pdf_path"] == "/tmp/cl.pdf"


# ===========================================================================
# Meta routes
# ===========================================================================


class TestMetaRoutes:
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_config_returns_fields(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "backend" in data
        assert "output_dir" in data


# ===========================================================================
# Job routes
# ===========================================================================


class TestListJobs:
    async def test_list_jobs_empty(self, client):
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_jobs_after_upsert(self, client, db):
        await _insert_job(db)
        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        jobs = resp.json()
        assert len(jobs) == 1
        assert jobs[0]["company"] == "Acme"


class TestGetJob:
    async def test_get_job_not_found(self, client):
        resp = await client.get("/api/jobs/nope")
        assert resp.status_code == 404

    async def test_get_job_full_includes_documents(self, client, db):
        await _insert_job(db)
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        data = resp.json()
        assert "documents" in data
        assert isinstance(data["documents"], list)


class TestAnswerFollowUp:
    async def test_answer_bad_job_404(self, client):
        resp = await client.post(
            "/api/jobs/doesnotexist00/answer",
            json={"follow_up_id": 1, "text": "hello"},
        )
        assert resp.status_code == 404


class TestApproveJob:
    async def test_approve_non_review_returns_400(self, client, db):
        """A pending job cannot be approved — expects 400."""
        await _insert_job(db)  # state=pending by default
        resp = await client.post("/api/jobs/aabbccdd00112233/approve")
        assert resp.status_code == 400

    async def test_approve_does_not_render(self, client, db, tmp_path, monkeypatch):
        """Approve only transitions review -> approved; it reads pre-existing pdf_path
        values and does not invoke the renderer at all (rendering happens on review-entry
        — see CLAUDE.md -> "Renderer invocation")."""
        from tests.backend.fakes.fake_renderer import FakeRenderer

        fake_renderer = FakeRenderer()
        monkeypatch.setattr("jsa.api.routes_jobs.renderer_for", lambda name: fake_renderer)

        # Insert job in review state with both documents, pdf_path already set (as if
        # pre-rendered on review-entry).
        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.review
            cv_doc = Document(
                job_id=job.id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# Adjusted CV",
                pdf_path=str(tmp_path / "cv.pdf"),
            )
            cl_doc = Document(
                job_id=job.id,
                stage=Stage.cover_letter,
                version=1,
                markdown="# Cover Letter",
                pdf_path=str(tmp_path / "cover_letter.pdf"),
            )
            session.add(cv_doc)
            session.add(cl_doc)
            await session.commit()

        resp = await client.post("/api/jobs/aabbccdd00112233/approve")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cv_pdf_path"] == str(tmp_path / "cv.pdf")
        assert data["cl_pdf_path"] == str(tmp_path / "cover_letter.pdf")
        assert fake_renderer.calls == []

        async with db() as session:
            refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.approved


class TestApproveCvJob:
    async def test_approve_cv_non_cv_review_returns_400(self, client, db):
        """A pending job cannot be approve-cv'd — expects 400."""
        await _insert_job(db)  # state=pending by default
        resp = await client.post("/api/jobs/aabbccdd00112233/approve-cv")
        assert resp.status_code == 400

    async def test_approve_from_cv_review_via_final_approve_returns_400(self, client, db):
        """The FINAL approve endpoint must reject a cv_review job — it's not final review."""
        await _insert_job(db, state=JobState.cv_review)
        resp = await client.post("/api/jobs/aabbccdd00112233/approve")
        assert resp.status_code == 400

    async def test_approve_cv_happy_path(self, client, db, tmp_path):
        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.cv_review
            cv_doc = Document(
                job_id=job.id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# Adjusted CV",
                pdf_path=str(tmp_path / "cv.pdf"),
                docx_path=str(tmp_path / "cv.docx"),
            )
            session.add(cv_doc)
            await session.commit()

        resp = await client.post(f"/api/jobs/{job.id}/approve-cv")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["pdf_path"] == str(tmp_path / "cv.pdf")
        assert data["docx_path"] == str(tmp_path / "cv.docx")

        async with db() as session:
            refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_done

    async def test_approve_cv_no_document_returns_400(self, client, db):
        await _insert_job(db, state=JobState.cv_review)
        resp = await client.post("/api/jobs/aabbccdd00112233/approve-cv")
        assert resp.status_code == 400

    async def test_approve_cv_with_unconsumed_revision_returns_400(self, client, db):
        """The orphaned-revision guard: cv_review + unconsumed RevisionRequest → 400."""
        from jsa.db.models import RevisionRequest

        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.cv_review
            cv_doc = Document(
                job_id=job.id, stage=Stage.cv_adjust, version=1, markdown="# CV",
            )
            session.add(cv_doc)
            rr = RevisionRequest(
                job_id=job.id,
                target=Stage.cv_adjust,
                instruction="Shorten it",
                origin_state="cv_review",
                consumed_at=None,
            )
            session.add(rr)
            await session.commit()

        resp = await client.post(f"/api/jobs/{job.id}/approve-cv")
        assert resp.status_code == 400

        async with db() as session:
            refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review  # unchanged


class TestExportJob:
    async def test_export_calls_renderer(self, client, db, tmp_path, monkeypatch):
        """POST /api/jobs/{id}/export re-renders both documents via the renderer."""
        from tests.backend.fakes.fake_renderer import FakeRenderer

        fake_renderer = FakeRenderer()
        monkeypatch.setattr("jsa.api.routes_jobs.renderer_for", lambda name: fake_renderer)

        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.approved
            cv_doc = Document(
                job_id=job.id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# Adjusted CV",
            )
            cl_doc = Document(
                job_id=job.id,
                stage=Stage.cover_letter,
                version=1,
                markdown="# Cover Letter",
            )
            session.add(cv_doc)
            session.add(cl_doc)
            await session.commit()

        resp = await client.post(
            f"/api/jobs/{job.id}/export", json={"format": "pdf"}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "cv_path" in data
        assert "cl_path" in data
        assert len(fake_renderer.calls) == 2

    async def test_export_cv_review_renders_only_cv(self, client, db, tmp_path, monkeypatch):
        """A cv_review job has no cover letter yet — export renders only the CV."""
        from tests.backend.fakes.fake_renderer import FakeRenderer

        fake_renderer = FakeRenderer()
        monkeypatch.setattr("jsa.api.routes_jobs.renderer_for", lambda name: fake_renderer)

        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.cv_review
            cv_doc = Document(
                job_id=job.id, stage=Stage.cv_adjust, version=1, markdown="# Adjusted CV",
            )
            session.add(cv_doc)
            await session.commit()

        resp = await client.post(
            f"/api/jobs/{job.id}/export", json={"format": "pdf"}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "cv_path" in data
        assert "cl_path" not in data
        assert len(fake_renderer.calls) == 1

    async def test_export_pending_returns_409(self, client, db):
        await _insert_job(db)  # state=pending
        resp = await client.post(
            "/api/jobs/aabbccdd00112233/export", json={"format": "pdf"}
        )
        assert resp.status_code == 409


class TestReviseJob:
    async def test_revise_non_review_returns_400(self, client, db):
        """A pending job cannot be revised — expects 400."""
        await _insert_job(db)  # state=pending by default
        resp = await client.post(
            "/api/jobs/aabbccdd00112233/revise",
            json={"target": "cv", "text": "Make it shorter"},
        )
        assert resp.status_code == 400

    async def test_revise_cv_from_cv_review_sets_origin_state(self, client, db):
        from sqlalchemy import select as sa_select
        from jsa.db.models import RevisionRequest

        await _insert_job(db, state=JobState.cv_review)
        resp = await client.post(
            "/api/jobs/aabbccdd00112233/revise",
            json={"target": "cv", "text": "Make it shorter"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["state"] == "cv_review"
        assert data["current_stage"] == "revising_cv"

        async with db() as session:
            result = await session.execute(
                sa_select(RevisionRequest).where(
                    RevisionRequest.job_id == "aabbccdd00112233"
                )
            )
            rr = result.scalar_one()
        assert rr.origin_state == "cv_review"

    async def test_revise_cl_from_cv_review_returns_400(self, client, db):
        """No cover letter exists yet at the CV gate — revising it is rejected."""
        await _insert_job(db, state=JobState.cv_review)
        resp = await client.post(
            "/api/jobs/aabbccdd00112233/revise",
            json={"target": "cl", "text": "Punch up the opening"},
        )
        assert resp.status_code == 400

    async def test_revise_cv_from_review_sets_origin_state(self, client, db):
        from sqlalchemy import select as sa_select
        from jsa.db.models import RevisionRequest

        await _insert_job(db, state=JobState.review)
        resp = await client.post(
            "/api/jobs/aabbccdd00112233/revise",
            json={"target": "cv", "text": "Make it shorter"},
        )
        assert resp.status_code == 200, resp.text

        async with db() as session:
            result = await session.execute(
                sa_select(RevisionRequest).where(
                    RevisionRequest.job_id == "aabbccdd00112233"
                )
            )
            rr = result.scalar_one()
        assert rr.origin_state == "review"


class TestResetJob:
    async def test_reset_failed_job(self, client, db):
        """Resetting a failed job should transition it to pending."""
        await _insert_job(db, state=JobState.failed)
        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "pending"

    async def test_reset_non_failed_returns_400(self, client, db):
        """Resetting a pending job should fail with 400."""
        await _insert_job(db)  # state=pending
        resp = await client.post("/api/jobs/aabbccdd00112233/reset")
        assert resp.status_code == 400


class TestIgnoreFit:
    async def _seed_unfit(self, db, reason="Unrelated field."):
        await _insert_job(db, state=JobState.unfit)
        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            job.fit_reason = reason
            await session.commit()

    async def test_fit_reason_in_serializer(self, client, db):
        await self._seed_unfit(db, reason="CV is for a nurse; role is backend.")
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        assert resp.json()["fit_reason"] == "CV is for a nurse; role is backend."

    async def test_ignore_fit_transitions_to_fit_done(self, client, db):
        await self._seed_unfit(db)
        resp = await client.post("/api/jobs/aabbccdd00112233/ignore-fit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "fit_done"
        assert data["fit_reason"] is None

    async def test_ignore_fit_non_unfit_returns_400(self, client, db):
        await _insert_job(db)  # state=pending
        resp = await client.post("/api/jobs/aabbccdd00112233/ignore-fit")
        assert resp.status_code == 400

    async def test_dismiss_from_unfit(self, client, db):
        await self._seed_unfit(db)
        resp = await client.post("/api/jobs/aabbccdd00112233/dismiss")
        assert resp.status_code == 200
        assert resp.json()["state"] == "dismissed"


class TestModelDisplay:
    """Phase 5 of the model-fallback-ladder plan: model_name/effective_model in the
    job DTO. See CLAUDE.md's "Model ladder" -> "Job-row display, one-way"."""

    async def test_never_hopped_effective_model_is_backend_flat_default(self, client, db):
        # No model_name (never hopped) and no runtime UI selection -> falls through to
        # the backend's flat default (Settings.model for claude-cli == "claude-haiku-4-5").
        await _insert_job(db, state=JobState.pending)
        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            job.backend_name = "claude-cli"
            await session.commit()

        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_name"] is None
        assert data["effective_model"] == "claude-haiku-4-5"

    async def test_hopped_effective_model_is_the_pinned_rung(self, client, db):
        await _insert_job(db, state=JobState.pending)
        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            job.backend_name = "opencode-go"
            job.model_name = "kimi-k2.6"
            job.model_hops = 1
            await session.commit()

        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_name"] == "kimi-k2.6"
        assert data["effective_model"] == "kimi-k2.6"

    async def test_no_backend_yet_effective_model_is_none(self, client, db):
        # backend_name is None (job never dispatched) -> nothing to resolve against.
        await _insert_job(db, state=JobState.queued)
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        data = resp.json()
        assert data["model_name"] is None
        assert data["effective_model"] is None

    async def test_list_jobs_summary_also_carries_effective_model(self, client, db):
        await _insert_job(db, state=JobState.pending)
        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            job.backend_name = "claude-cli"
            await session.commit()

        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["effective_model"] == "claude-haiku-4-5"


class TestLaunchJob:
    """Manual job launch: queued → pending, snapshotting the global language."""

    async def test_launch_transitions_queued_to_pending(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.post("/api/jobs/aabbccdd00112233/launch")
        assert resp.status_code == 200
        assert resp.json()["state"] == "pending"

    async def test_launch_snapshots_current_global_language(self, client, db):
        await _insert_job(db, state=JobState.queued)
        put_resp = await client.put("/api/preferences", json={"language": "es"})
        assert put_resp.status_code == 200

        resp = await client.post("/api/jobs/aabbccdd00112233/launch")
        assert resp.status_code == 200
        assert resp.json()["language"] == "es"

        # Changing the global preference afterward must not affect the launched job.
        await client.put("/api/preferences", json={"language": "de"})
        async with db() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            assert job.language == "es"

    async def test_launch_non_queued_returns_400(self, client, db):
        await _insert_job(db, state=JobState.pending)
        resp = await client.post("/api/jobs/aabbccdd00112233/launch")
        assert resp.status_code == 400

    async def test_launch_not_found_returns_404(self, client, db):
        resp = await client.post("/api/jobs/doesnotexist0000/launch")
        assert resp.status_code == 404


class TestLaunchAllJobs:
    """The sidebar's 'Launch All' — launches every currently-queued job in one shot."""

    async def test_launch_all_launches_every_queued_job(self, client, db):
        await _insert_job(db, state=JobState.queued, job_id="aaaa000000000001")
        await _insert_job(db, state=JobState.queued, job_id="aaaa000000000002")
        # A non-queued job must be left untouched.
        await _insert_job(db, state=JobState.review, job_id="aaaa000000000003")

        resp = await client.post("/api/jobs/launch-all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert set(body["launched"]) == {"aaaa000000000001", "aaaa000000000002"}

        async with db() as session:
            job1 = await repo.get_job(session, "aaaa000000000001")
            job2 = await repo.get_job(session, "aaaa000000000002")
            job3 = await repo.get_job(session, "aaaa000000000003")
            assert job1.state == JobState.pending
            assert job2.state == JobState.pending
            assert job3.state == JobState.review  # untouched

    async def test_launch_all_with_no_queued_jobs_returns_zero(self, client, db):
        await _insert_job(db, state=JobState.pending)
        resp = await client.post("/api/jobs/launch-all")
        assert resp.status_code == 200
        assert resp.json() == {"launched": [], "count": 0}


class TestGetDocument:
    async def test_get_document_not_found(self, client, db):
        """A job with no documents should return 404 for the document endpoint."""
        await _insert_job(db)
        resp = await client.get("/api/jobs/aabbccdd00112233/document/cv_adjust")
        assert resp.status_code == 404

    async def test_get_document_returns_markdown(self, client, db):
        """After inserting a document, the endpoint should return it."""
        async with db() as session:
            job_data = _job_data()
            job = await repo.upsert_job(session, job_data)
            job.state = JobState.review
            doc = Document(
                job_id=job.id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# My CV",
            )
            session.add(doc)
            await session.commit()

        resp = await client.get("/api/jobs/aabbccdd00112233/document/cv_adjust")
        assert resp.status_code == 200
        data = resp.json()
        assert data["markdown"] == "# My CV"
        assert data["version"] == 1


# ===========================================================================
# Recovery sweep — direct DB tests (no HTTP)
# ===========================================================================


@pytest.fixture
async def mem_session_factory():
    """In-memory SQLite with StaticPool — shared across sessions in one test."""
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


async def _insert_running_job(
    sf, job_id: str = "aabbccdd00112233", current_stage: Stage = Stage.cv_adjust
) -> str:
    """Insert a job in running state for recovery tests."""
    async with sf() as session:
        job = Job(
            id=job_id,
            company="Acme",
            role="Engineer",
            link="https://acme.com",
            tier="A",
            jd="JD text",
            jd_hash="hash0000deadbeef",
            cv_text="CV text",
            state=JobState.running,
            current_stage=current_stage,
        )
        session.add(job)
        await session.commit()
    return job_id


class TestRecoverySweep:
    async def test_recovery_running_no_docs_becomes_pending(self, mem_session_factory):
        """A running job with no documents should revert to pending."""
        job_id = await _insert_running_job(mem_session_factory)

        async with mem_session_factory() as session:
            await repo.recovery_sweep(session)

        async with mem_session_factory() as session:
            job = await repo.get_job(session, job_id)
            assert job.state == JobState.pending
            assert job.current_stage is None

    async def test_recovery_running_cv_adjust_with_stale_cv_doc_becomes_pending(
        self, mem_session_factory
    ):
        """A running(cv_adjust) job must rewind to pending, NEVER cv_done, even if a
        cv_adjust Document already exists from an earlier job cycle.

        A completed cv_adjust run always transitions atomically past `running` straight
        to `cv_review` in the same checkpoint — being caught here mid-run means THIS
        attempt did not complete. Treating a leftover Document as proof of completion
        (the pre-two-lane-split heuristic) would silently bypass the cv_review approval
        gate for a CV the user never actually approved this cycle. Regression test for
        that gate-bypass — see jsa/db/repo.py::recovery_sweep.
        """
        job_id = await _insert_running_job(mem_session_factory)

        async with mem_session_factory() as session:
            doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# CV",
            )
            session.add(doc)
            await session.commit()

        async with mem_session_factory() as session:
            await repo.recovery_sweep(session)

        async with mem_session_factory() as session:
            job = await repo.get_job(session, job_id)
            assert job.state == JobState.pending
            assert job.current_stage is None

    async def test_recovery_running_cover_letter_with_cl_doc_becomes_cl_done(
        self, mem_session_factory
    ):
        """A running(cover_letter) job that has a cl document should revert to cl_done.

        Reaching running(cover_letter) at all already required cv_done (the state
        machine gates it), so landing on cl_done here cannot bypass the CV approval
        gate — unlike a plain running(cv_adjust) crash, see the sibling test above.
        """
        job_id = await _insert_running_job(
            mem_session_factory, current_stage=Stage.cover_letter
        )

        async with mem_session_factory() as session:
            cv_doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# CV",
            )
            cl_doc = Document(
                job_id=job_id,
                stage=Stage.cover_letter,
                version=1,
                markdown="# Cover Letter",
            )
            session.add(cv_doc)
            session.add(cl_doc)
            await session.commit()

        async with mem_session_factory() as session:
            await repo.recovery_sweep(session)

        async with mem_session_factory() as session:
            job = await repo.get_job(session, job_id)
            assert job.state == JobState.cl_done
            assert job.current_stage is None

    async def test_recovery_leaves_awaiting_input_untouched(self, mem_session_factory):
        """Jobs in awaiting_input should not be modified by the recovery sweep."""
        async with mem_session_factory() as session:
            job = Job(
                id="aabbccdd00112233",
                company="Acme",
                role="Engineer",
                link="https://acme.com",
                tier="A",
                jd="JD text",
                jd_hash="hash0000deadbeef",
                cv_text="CV text",
                state=JobState.awaiting_input,
                current_stage=Stage.cv_adjust,
            )
            session.add(job)
            await session.commit()

        async with mem_session_factory() as session:
            await repo.recovery_sweep(session)

        async with mem_session_factory() as session:
            job = await repo.get_job(session, "aabbccdd00112233")
            assert job.state == JobState.awaiting_input
            assert job.current_stage == Stage.cv_adjust


class TestFollowUpToDictSuggestedReplies:
    """FollowUp -> dict serialization of suggested_replies (agent chat upgrade Phase 4)."""

    def test_legacy_null_column_serializes_as_none(self):
        from jsa.api.routes_jobs import _follow_up_to_dict

        fu = FollowUp(
            id=1,
            job_id="j1",
            stage=Stage.cv_adjust,
            question="q?",
            answer=None,
            suggested_replies=None,
        )
        d = _follow_up_to_dict(fu)
        assert d["suggested_replies"] is None

    def test_json_encoded_column_round_trips(self):
        import json

        from jsa.api.routes_jobs import _follow_up_to_dict

        fu = FollowUp(
            id=1,
            job_id="j1",
            stage=Stage.cv_adjust,
            question="q?",
            answer=None,
            suggested_replies=json.dumps(["Option A", "Option B"]),
        )
        d = _follow_up_to_dict(fu)
        assert d["suggested_replies"] == ["Option A", "Option B"]
