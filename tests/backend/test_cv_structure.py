"""Phase 2A — standalone CV-structure store + API (CV Structure Editor backend).

Covers the job-less store (`jsa/store/cv_structure.py`) and the three routes
(`jsa/api/routes_cv_structure.py`): GET (404 when empty), PUT (validate + persist, 422 on the
schema hard gates), and POST infer (one-shot inference via a FakeAgentBackend, with the
`infer_progress` events broadcast on the bus).

Uses the FakeAgentBackend (no real agent calls) and a tmp_path DB so `Settings.cv_structure_path`
is isolated per test. Follows the test_api.py fixture shape.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.agents.base import AgentReply
from jsa.config import Settings
from jsa.events.bus import bus
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema import CVDocument
from jsa.server import create_app
from jsa.store import cv_structure
from tests.backend.fakes.fake_backend import FakeAgentBackend

# --- sample payloads -------------------------------------------------------------------

_VALID_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com", "location": "Berlin",
                "links": ["github.com/janedoe"]},
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"heading": "Senior Engineer", "subheading": "Acme", "dates": "2020–Present",
             "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker"]},
    ],
}

# A cover letter wearing CV shape — must trip CVDocument._not_a_cover_letter (≥2 formulas).
_COVER_LETTER_AS_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the role.",
        "I would welcome discussing it further. Sincerely, Jane",
    ]}],
}


# --- fixtures (mirror test_api.py) ------------------------------------------------------

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


# --- store (pure, no HTTP) --------------------------------------------------------------

class TestStore:
    async def test_load_none_when_absent(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        assert cv_structure.structure_path(settings) == tmp_path / "cv_structure.json"
        assert await cv_structure.load(settings) is None

    async def test_save_then_load_roundtrip(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        cv = CVDocument.model_validate(_VALID_CV)
        await cv_structure.save(settings, cv)
        assert cv_structure.structure_path(settings).exists()
        loaded = await cv_structure.load(settings)
        assert loaded is not None
        assert loaded.contact.name == "Jane Doe"
        assert [s.name for s in loaded.sections] == ["Summary", "Experience", "Skills"]


# --- GET / PUT --------------------------------------------------------------------------

class TestGetPut:
    async def test_get_404_when_empty(self, client):
        resp = await client.get("/api/cv-structure")
        assert resp.status_code == 404

    async def test_put_valid_persists_and_get_returns(self, client):
        put = await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        assert put.status_code == 200
        assert put.json()["structured"]["contact"]["name"] == "Jane Doe"

        get = await client.get("/api/cv-structure")
        assert get.status_code == 200
        assert get.json()["structured"]["contact"]["name"] == "Jane Doe"

    async def test_put_cover_letter_as_cv_rejected_422(self, client):
        resp = await client.put("/api/cv-structure", json={"structured": _COVER_LETTER_AS_CV})
        assert resp.status_code == 422
        assert "cover letter" in resp.json()["detail"].lower()
        # Nothing was persisted.
        assert (await client.get("/api/cv-structure")).status_code == 404

    async def test_put_missing_name_rejected_422(self, client):
        bad = {"contact": {"email": "x@y.com"}, "sections": [{"name": "Skills", "items": ["Go"]}]}
        resp = await client.put("/api/cv-structure", json={"structured": bad})
        assert resp.status_code == 422


# --- POST infer -------------------------------------------------------------------------

class TestInfer:
    async def test_infer_happy_path(self, test_app, client, monkeypatch):
        # The backend returns the CVDocument JSON as the FINAL payload (content = inner text).
        import json as _json
        reply = AgentReply(raw="x", content=_json.dumps(_VALID_CV), kind="final")
        test_app.state.backend_factory = lambda name: FakeAgentBackend([reply])
        # Skip real pypdf/docx extraction — the file bytes are irrelevant to this test.
        monkeypatch.setattr(
            "jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text"
        )

        q = bus.subscribe()
        try:
            resp = await client.post(
                "/api/cv-structure/infer",
                files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )
        finally:
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            bus.unsubscribe(q)

        assert resp.status_code == 200
        body = resp.json()
        assert body["structured"]["contact"]["name"] == "Jane Doe"
        assert body["task_id"]

        # Inference is NOT persisted — GET still empty until the editor PUTs.
        assert (await client.get("/api/cv-structure")).status_code == 404

        # All five steps were broadcast, ending with a terminal done event.
        progress = [e for e in events if e["type"] == "infer_progress"]
        assert {e["step"] for e in progress} == {1, 2, 3, 4, 5}
        assert any(e["status"] == "done" for e in progress)

    async def test_infer_invalid_output_422(self, test_app, client, monkeypatch):
        reply = AgentReply(raw="x", content="not json at all", kind="final")
        test_app.state.backend_factory = lambda name: FakeAgentBackend([reply])
        monkeypatch.setattr(
            "jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text"
        )
        resp = await client.post(
            "/api/cv-structure/infer",
            files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert resp.status_code == 422

    async def test_infer_agent_timeout_emits_step4_error(self, test_app, client, monkeypatch):
        from jsa.agents.base import AgentTimeout

        class _TimeoutBackend(FakeAgentBackend):
            async def start_session(self, system_prompt, initial_user_msg):
                raise AgentTimeout("timed out")

        test_app.state.backend_factory = lambda name: _TimeoutBackend([])
        monkeypatch.setattr(
            "jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text"
        )

        q = bus.subscribe()
        try:
            resp = await client.post(
                "/api/cv-structure/infer",
                files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )
        finally:
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            bus.unsubscribe(q)

        assert resp.status_code == 422
        step4_errors = [
            e for e in events
            if e["type"] == "infer_progress" and e["step"] == 4 and e["status"] == "error"
        ]
        assert step4_errors, "AgentTimeout must emit a step-4 error infer_progress event"

    async def test_infer_generic_backend_error_emits_step4_error(self, test_app, client, monkeypatch):
        class _BrokenBackend(FakeAgentBackend):
            async def start_session(self, system_prompt, initial_user_msg):
                raise RuntimeError("connection refused")

        test_app.state.backend_factory = lambda name: _BrokenBackend([])
        monkeypatch.setattr(
            "jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text"
        )

        q = bus.subscribe()
        try:
            resp = await client.post(
                "/api/cv-structure/infer",
                files={"file": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
            )
        finally:
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            bus.unsubscribe(q)

        assert resp.status_code == 422
        step4_errors = [
            e for e in events
            if e["type"] == "infer_progress" and e["step"] == 4 and e["status"] == "error"
        ]
        assert step4_errors, "generic backend Exception must emit a step-4 error infer_progress event"
