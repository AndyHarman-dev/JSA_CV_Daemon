"""`POST /api/jobs/{id}/save-cv-as-deck` — copy a job's tailored CV into a new base CV.

Covers:
- Happy path: the latest `cv_adjust` Document's `structured` JSON lands in a brand-new deck
  named "{company} · {role}", with the right `auto_title`/`has_cv`, and it is the LATEST
  version that gets copied, not the first.
- The new deck never takes over an existing default; repeat saves mint separate decks.
- Gating is on "a cv_adjust Document exists", not on a list of states — a pre-CV job 404s,
  while a job past the CV (even `approved`) succeeds.
- A Document with no `structured` JSON (legacy rows) is a 409; one that fails CVDocument
  validation is a 422 and writes nothing.
- The job itself is untouched (no state change), and `orchestrator.kick()` fires, like
  every other deck write.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Document, JobState, Stage
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema import CVDocument
from jsa.server import create_app
from jsa.store import cv_decks
from tests.backend.fakes.fake_backend import FakeAgentBackend

_JOB_ID = "aabbccdd00112233"

_TAILORED_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [
        {"name": "Summary", "text": "Backend engineer tailored for Acme."},
        {"name": "Skills", "items": ["Python", "Go"]},
    ],
}

_TAILORED_CV_V2 = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "Summary", "text": "Revised: backend engineer for Acme."}],
}

_BASE_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "Summary", "text": "The untailored base CV."}],
}

# A cover letter wearing CV shape — must trip CVDocument._not_a_cover_letter (≥2 formulas).
_COVER_LETTER_AS_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the role.",
        "I would welcome discussing it further. Sincerely, Jane",
    ]}],
}


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backend="claude-cli",
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
    return test_app.state.session_factory


@pytest.fixture
def settings(test_app):
    return test_app.state.settings


async def _insert_job(db, *, state: JobState, cv_versions: list[dict | None] = ()) -> None:
    """One job in ``state`` with a cv_adjust Document per entry of ``cv_versions``
    (version 1, 2, ...). A ``None`` entry stores a Document with no ``structured`` JSON."""
    async with db() as session:
        job = await repo.upsert_job(
            session,
            dict(
                id=_JOB_ID,
                company="Acme",
                role="Engineer",
                link="https://acme.com/job",
                tier="A",
                jd="Job description text",
                jd_hash="hash0000deadbeef",
            ),
        )
        job.state = state
        for version, structured in enumerate(cv_versions, start=1):
            session.add(
                Document(
                    job_id=job.id,
                    stage=Stage.cv_adjust,
                    version=version,
                    markdown="# CV",
                    structured=json.dumps(structured) if structured is not None else None,
                )
            )
        await session.commit()


class TestHappyPath:
    async def test_copies_latest_cv_into_a_new_named_deck(self, client, db, settings):
        await _insert_job(
            db, state=JobState.review, cv_versions=[_TAILORED_CV, _TAILORED_CV_V2]
        )

        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 201, resp.text
        deck = resp.json()["deck"]
        assert deck["name"] == "Acme · Engineer"
        assert deck["auto_title"] == "Jane Doe"
        assert deck["has_cv"] is True

        stored = await cv_decks.load_deck(settings, deck["id"])
        assert stored is not None
        assert stored.model_dump() == (
            await client.get(f"/api/cv-decks/{deck['id']}")
        ).json()["structured"]
        # The latest version (the one ReviewPane previews), not v1.
        assert stored.sections[0].text == "Revised: backend engineer for Acme."

    async def test_does_not_become_default_and_repeats_mint_new_decks(
        self, client, db, settings
    ):
        base = await cv_decks.create_deck_from_cv(
            settings, CVDocument.model_validate(_BASE_CV)
        )
        await _insert_job(db, state=JobState.cv_review, cv_versions=[_TAILORED_CV])

        first = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        second = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert first.status_code == second.status_code == 201
        assert first.json()["deck"]["is_default"] is False
        assert first.json()["deck"]["id"] != second.json()["deck"]["id"]

        listed = (await client.get("/api/cv-decks")).json()
        assert listed["default_id"] == base.id
        assert len(listed["decks"]) == 3

    async def test_job_state_is_untouched(self, client, db):
        await _insert_job(db, state=JobState.cv_review, cv_versions=[_TAILORED_CV])

        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 201

        async with db() as session:
            job = await repo.get_job(session, _JOB_ID)
        assert job.state == JobState.cv_review

    @pytest.mark.parametrize(
        "state",
        [JobState.cv_review, JobState.running, JobState.awaiting_input, JobState.review,
         JobState.approved, JobState.failed, JobState.dismissed],
    )
    async def test_any_state_with_a_cv_document_is_allowed(self, client, db, state):
        await _insert_job(db, state=state, cv_versions=[_TAILORED_CV])
        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 201, resp.text

    async def test_kicks_orchestrator(self, client, db, monkeypatch):
        await _insert_job(db, state=JobState.review, cv_versions=[_TAILORED_CV])

        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 201
        assert calls == ["kick"]


class TestErrors:
    async def test_unknown_job_404(self, client):
        resp = await client.post("/api/jobs/ffffffffffffffff/save-cv-as-deck")
        assert resp.status_code == 404

    async def test_no_cv_yet_404_and_nothing_written(self, client, db):
        await _insert_job(db, state=JobState.pending)
        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 404
        assert (await client.get("/api/cv-decks")).json()["decks"] == []

    async def test_document_without_structured_json_409(self, client, db):
        await _insert_job(db, state=JobState.review, cv_versions=[None])
        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 409
        assert (await client.get("/api/cv-decks")).json()["decks"] == []

    async def test_invalid_structure_422_and_nothing_written(self, client, db):
        await _insert_job(db, state=JobState.review, cv_versions=[_COVER_LETTER_AS_CV])
        resp = await client.post(f"/api/jobs/{_JOB_ID}/save-cv-as-deck")
        assert resp.status_code == 422
        assert "cover letter" in resp.json()["detail"].lower()
        assert (await client.get("/api/cv-decks")).json()["decks"] == []
