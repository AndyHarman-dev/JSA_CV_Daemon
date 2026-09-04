"""Phase 3 — `Job.base_cv_id` + `PUT /api/jobs/{id}/base-cv` (CV Decks feature).

Covers:
- DTO exposure of `base_cv_id` on the job JSON.
- Assign / reassign / unassign a deck on a `queued` job.
- 409 when the job isn't `queued` (already launched).
- 422 when the target deck is unknown, or a real-but-empty (`has_cv=False`) slot.
- `jsa.db.repo.clear_base_cv_assignments` — direct DB test (no HTTP, no route wiring;
  the DELETE-route wiring itself belongs to a different phase/agent, see the Phase 3
  task notes and CLAUDE.md).

Mirrors `tests/backend/test_api.py`'s fixture shape (its own local `test_app`/`client`/`db`
fixtures — no shared conftest.py exists for these yet).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Job, JobState
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema import CVDocument
from jsa.server import create_app
from jsa.store import cv_decks

_VALID_CV = {
    "contact": {
        "name": "Jane Doe", "email": "jane@x.com", "location": "Berlin",
        "links": ["github.com/janedoe"],
    },
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"heading": "Senior Engineer", "subheading": "Acme", "dates": "2020-Present",
             "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker"]},
    ],
}


# ---------------------------------------------------------------------------
# Fixtures (local to this file — see module docstring)
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    """FastAPI app with Orchestrator.run patched to a no-op and a FakeAgentBackend,
    exactly like test_api.py's fixture — this file never dispatches the pipeline."""
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
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory for seeding data after startup has run."""
    return test_app.state.session_factory


@pytest.fixture
def settings(test_app) -> Settings:
    """The same Settings instance the app was built with — usable immediately, no
    startup dependency (cv_decks_path/cv_decks_dir are pure derivations of db_path)."""
    return test_app.state.settings


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
        id=job_id, company=company, role=role, link=link, tier=tier,
        jd=jd, jd_hash=jd_hash, cv_text=cv_text,
    )


async def _insert_job(session_factory, *, state: JobState = JobState.queued, **overrides) -> Job:
    """Insert a job via upsert_job and force its state (upsert_job always creates fresh
    jobs as `queued`, which happens to already be this fixture's default)."""
    data = _job_data(**overrides)
    async with session_factory() as session:
        job = await repo.upsert_job(session, data)
        await session.commit()
        job_id = job.id
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        job.state = state
        await session.commit()
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        return job


async def _make_deck(settings: Settings, *, has_cv: bool = True) -> str:
    """Register a new deck; optionally save a valid CV into it (has_cv=True)."""
    meta = await cv_decks.create_deck(settings)
    if has_cv:
        cv = CVDocument.model_validate(_VALID_CV)
        await cv_decks.save_deck(settings, meta.id, cv)
    return meta.id


# ===========================================================================
# DTO exposure
# ===========================================================================


class TestBaseCvDtoExposure:
    async def test_default_is_null(self, client, db):
        await _insert_job(db)
        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] is None

    async def test_assigned_value_is_exposed(self, client, db, settings):
        await _insert_job(db)
        deck_id = await _make_deck(settings)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_id}
        )
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] == deck_id

        resp = await client.get("/api/jobs/aabbccdd00112233")
        assert resp.json()["base_cv_id"] == deck_id

    async def test_list_jobs_summary_also_exposes_it(self, client, db, settings):
        await _insert_job(db)
        deck_id = await _make_deck(settings)
        await client.put("/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_id})

        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        [job] = resp.json()
        assert job["base_cv_id"] == deck_id


# ===========================================================================
# PUT /api/jobs/{id}/base-cv
# ===========================================================================


class TestAssignBaseCv:
    async def test_assign_on_queued_succeeds(self, client, db, settings):
        await _insert_job(db)
        deck_id = await _make_deck(settings)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_id}
        )
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] == deck_id

    async def test_reassign_overwrites_previous_value(self, client, db, settings):
        await _insert_job(db)
        deck_a = await _make_deck(settings)
        deck_b = await _make_deck(settings)

        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_a}
        )
        assert resp.json()["base_cv_id"] == deck_a

        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_b}
        )
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] == deck_b

    async def test_unassign_sets_null(self, client, db, settings):
        await _insert_job(db)
        deck_id = await _make_deck(settings)
        await client.put("/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_id})

        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": None}
        )
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] is None

    async def test_404_unknown_job(self, client, db, settings):
        deck_id = await _make_deck(settings)
        resp = await client.put(
            "/api/jobs/deadbeefdeadbeef/base-cv", json={"deck_id": deck_id}
        )
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "state",
        [JobState.running, JobState.cv_review, JobState.review, JobState.approved],
    )
    async def test_409_when_not_queued(self, client, db, settings, state):
        await _insert_job(db, state=state)
        deck_id = await _make_deck(settings)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": deck_id}
        )
        assert resp.status_code == 409
        assert "before launch" in resp.json()["detail"]

    async def test_422_unknown_deck_id(self, client, db):
        await _insert_job(db)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv",
            json={"deck_id": "0" * 32},
        )
        assert resp.status_code == 422

    async def test_422_deck_with_no_saved_cv(self, client, db, settings):
        """An empty, never-saved deck slot (has_cv=False) must not be assignable —
        otherwise cv_decks.resolve_path silently falls back to the default deck."""
        await _insert_job(db)
        empty_deck_id = await _make_deck(settings, has_cv=False)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv",
            json={"deck_id": empty_deck_id},
        )
        assert resp.status_code == 422

    async def test_null_deck_id_is_always_valid(self, client, db):
        """Unassigning (deck_id=None) never needs an index lookup and always succeeds
        from queued, even with zero decks registered anywhere."""
        await _insert_job(db)
        resp = await client.put(
            "/api/jobs/aabbccdd00112233/base-cv", json={"deck_id": None}
        )
        assert resp.status_code == 200
        assert resp.json()["base_cv_id"] is None


# ===========================================================================
# repo.clear_base_cv_assignments (direct DB test — no HTTP, no route wiring)
# ===========================================================================


class TestClearBaseCvAssignments:
    async def test_clears_queued_and_pending_rows(self, db):
        deck_id = "a" * 32
        queued = await _insert_job(db, job_id="1111111111111111", state=JobState.queued)
        pending = await _insert_job(db, job_id="2222222222222222", state=JobState.pending)

        async with db() as session:
            for job in (queued, pending):
                job = await repo.get_job(session, job.id)
                job.base_cv_id = deck_id
                session.add(job)
            await session.commit()

        async with db() as session:
            count = await repo.clear_base_cv_assignments(session, deck_id)
        assert count == 2

        async with db() as session:
            for job_id in ("1111111111111111", "2222222222222222"):
                job = await repo.get_job(session, job_id)
                assert job.base_cv_id is None

    async def test_leaves_review_and_approved_rows_untouched(self, db):
        deck_id = "b" * 32
        review = await _insert_job(db, job_id="3333333333333333", state=JobState.review)
        approved = await _insert_job(db, job_id="4444444444444444", state=JobState.approved)

        async with db() as session:
            for job in (review, approved):
                job = await repo.get_job(session, job.id)
                job.base_cv_id = deck_id
                session.add(job)
            await session.commit()

        async with db() as session:
            count = await repo.clear_base_cv_assignments(session, deck_id)
        assert count == 0

        async with db() as session:
            for job_id in ("3333333333333333", "4444444444444444"):
                job = await repo.get_job(session, job_id)
                assert job.base_cv_id == deck_id

    async def test_only_clears_matching_deck_id(self, db):
        target_deck = "c" * 32
        other_deck = "d" * 32
        target_job = await _insert_job(db, job_id="5555555555555555", state=JobState.queued)
        other_job = await _insert_job(db, job_id="6666666666666666", state=JobState.queued)

        async with db() as session:
            j1 = await repo.get_job(session, target_job.id)
            j1.base_cv_id = target_deck
            j2 = await repo.get_job(session, other_job.id)
            j2.base_cv_id = other_deck
            session.add_all([j1, j2])
            await session.commit()

        async with db() as session:
            count = await repo.clear_base_cv_assignments(session, target_deck)
        assert count == 1

        async with db() as session:
            j1 = await repo.get_job(session, target_job.id)
            j2 = await repo.get_job(session, other_job.id)
            assert j1.base_cv_id is None
            assert j2.base_cv_id == other_deck


# ===========================================================================
# DELETE /api/cv-decks/{id} wires up clear_base_cv_assignments
#
# Added by the supervising agent after Phases 2 and 3 landed: the route lives in
# Phase 2's file and the repo helper in Phase 3's, so neither parallel agent could
# write this test — it is the seam between them, and the seam is exactly where a
# silent gap would hide (the helper is unit-tested, the route is unit-tested, and
# nothing would notice if the route never called the helper).
# ===========================================================================


class TestDeleteDeckClearsAssignments:
    async def test_delete_clears_undispatched_and_spares_dispatched(
        self, client, db, settings
    ):
        deck_id = await _make_deck(settings)
        other_deck = await _make_deck(settings)

        queued = await _insert_job(db, job_id="1111111111111111", state=JobState.queued)
        pending = await _insert_job(db, job_id="2222222222222222", state=JobState.pending)
        approved = await _insert_job(db, job_id="3333333333333333", state=JobState.approved)
        untouched = await _insert_job(db, job_id="4444444444444444", state=JobState.queued)

        async with db() as s:
            for job, deck in (
                (queued, deck_id), (pending, deck_id),
                (approved, deck_id), (untouched, other_deck),
            ):
                row = await repo.get_job(s, job.id)
                row.base_cv_id = deck
            await s.commit()

        resp = await client.delete(f"/api/cv-decks/{deck_id}")
        assert resp.status_code == 204

        async with db() as s:
            assert (await repo.get_job(s, queued.id)).base_cv_id is None
            assert (await repo.get_job(s, pending.id)).base_cv_id is None
            # A dispatched job keeps the record of which base CV it was built from.
            assert (await repo.get_job(s, approved.id)).base_cv_id == deck_id
            # A job pointing at a different deck is never touched.
            assert (await repo.get_job(s, untouched.id)).base_cv_id == other_deck

    async def test_delete_of_unassigned_deck_is_harmless(self, client, db, settings):
        deck_id = await _make_deck(settings)
        await _make_deck(settings)
        job = await _insert_job(db, job_id="5555555555555555", state=JobState.queued)

        resp = await client.delete(f"/api/cv-decks/{deck_id}")
        assert resp.status_code == 204
        async with db() as s:
            assert (await repo.get_job(s, job.id)).base_cv_id is None
