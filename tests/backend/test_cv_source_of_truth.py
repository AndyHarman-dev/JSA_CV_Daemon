"""Tests for cv_structure.json as the single source of truth for CV content.

Covers:
- Orchestrator.run()'s gate: jobs stay pending while no CV structure exists; a saved
  structure + kick() unblocks without a restart.
- PUT /api/cv-structure calls orchestrator.kick() so the editor unblocks the gate live.
- GET /api/config reports cv_structure_exists.
- jsa.cli._bootstrap_cv_structure: seed-once, ignore-if-exists, proceed-if-neither,
  and fail-loud-on-InferError behavior.

Uses FakeAgentBackend (no real agent calls), mirroring test_orchestrator.py / test_cv_structure.py.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
import typer
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.cli import _bootstrap_cv_structure
from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Job, JobState
from jsa.pipeline.infer_structure import InferError
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema import CVDocument
from jsa.server import create_app
from jsa.store import cv_decks, cv_structure
from tests.backend.fakes.fake_backend import FakeAgentBackend

_VALID_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Skills", "items": ["Python", "Go"]},
    ],
}


def _final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


# ---------------------------------------------------------------------------
# Orchestrator gate
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
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


async def _insert_job(factory, job_id: str | None = None) -> Job:
    data = dict(
        id=job_id or uuid4().hex[:16],
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
    )
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.pending
        await s.commit()
    return job


class TestOrchestratorCvGate:
    async def test_job_stays_pending_while_structure_missing(self, session_factory, tmp_path):
        import asyncio

        structure_path = tmp_path / "cv_structure.json"
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([_final("FIT")]),
            cv_structure_path=structure_path,
        )
        orch_task = asyncio.create_task(orch.run())
        try:
            # Let the orchestrator start and settle into its gated wait before inserting,
            # mirroring test_orchestrator.py's TestKickUnblocksLoop pattern (avoids racing
            # the loop's own DB session against this insert).
            await asyncio.sleep(0.1)
            job = await _insert_job(session_factory)
            orch.kick()
            await asyncio.sleep(0.2)

            async with session_factory() as s:
                refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.pending  # never dispatched — gate is blocking

            # Now save the structure and kick — the job should be picked up.
            structure_path.write_text(
                json.dumps(_VALID_CV), encoding="utf-8"
            )
            orch.kick()

            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                async with session_factory() as s:
                    refreshed = await repo.get_job(s, job.id)
                if refreshed.state != JobState.pending:
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError(f"job never left pending; state={refreshed.state}")
                await asyncio.sleep(0.05)
            assert refreshed.state != JobState.pending
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(orch_task, timeout=5.0)

    async def test_job_stays_pending_not_failed_while_structure_corrupt(
        self, session_factory, tmp_path
    ):
        """A present-but-invalid cv_structure.json must block the gate exactly like a
        missing one — jobs stay pending, never failed. Regression test: the gate used to
        check only .exists(), so a corrupt file let jobs dispatch straight into an
        unguarded read() that raised and got the job marked failed."""
        import asyncio

        structure_path = tmp_path / "cv_structure.json"
        structure_path.write_text("{not valid json", encoding="utf-8")
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([_final("FIT")]),
            cv_structure_path=structure_path,
        )
        orch_task = asyncio.create_task(orch.run())
        try:
            await asyncio.sleep(0.1)
            job = await _insert_job(session_factory)
            orch.kick()
            await asyncio.sleep(0.3)

            async with session_factory() as s:
                refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.pending  # blocked, not failed
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(orch_task, timeout=5.0)

    async def test_gate_inert_when_no_structure_path(self, session_factory):
        """cv_structure_path=None (tests/legacy callers) must not block dispatch."""
        import asyncio

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([_final("FIT")]),
        )
        orch_task = asyncio.create_task(orch.run())
        try:
            await asyncio.sleep(0.1)
            job = await _insert_job(session_factory)
            orch.kick()

            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                async with session_factory() as s:
                    refreshed = await repo.get_job(s, job.id)
                if refreshed.state != JobState.pending:
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError("job never left pending with cv_structure_path=None")
                await asyncio.sleep(0.05)
            assert refreshed.state != JobState.pending
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(orch_task, timeout=5.0)


# ---------------------------------------------------------------------------
# HTTP: kick-on-save + /api/config signal
# ---------------------------------------------------------------------------


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


class TestKickOnSave:
    async def test_put_cv_structure_kicks_orchestrator(self, test_app, client, monkeypatch):
        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append(True))

        resp = await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        assert resp.status_code == 200
        assert calls == [True]


class TestConfigSignal:
    async def test_cv_structure_exists_reflects_store_state(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["cv_structure_exists"] is False

        put = await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        assert put.status_code == 200

        resp2 = await client.get("/api/config")
        assert resp2.json()["cv_structure_exists"] is True


# ---------------------------------------------------------------------------
# CLI bootstrap (_bootstrap_cv_structure)
# ---------------------------------------------------------------------------


class TestBootstrapCvStructure:
    async def test_seeds_and_saves_when_missing(self, tmp_path, monkeypatch):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        cv_path = tmp_path / "resume.pdf"
        cv_path.write_bytes(b"%PDF-1.4")

        monkeypatch.setattr(
            "jsa.cli.make_backend_factory",
            lambda s: (lambda name: FakeAgentBackend([_final(json.dumps(_VALID_CV))])),
        )
        monkeypatch.setattr("jsa.pipeline.infer_structure.load_cv", lambda path: "raw cv text")

        await _bootstrap_cv_structure(settings, cv_path)

        # Seeds a *deck*, not the legacy single file. Rewritten deliberately when decks
        # landed: the assertion below used to be `settings.cv_structure_path.exists()`.
        # Do not "fix" a future failure here by having the bootstrap also write the legacy
        # file — that resurrects the second source of truth decks exist to remove, and the
        # legacy path is now only ever *read* (once, by migrate_legacy).
        index = await cv_decks.load_index(settings)
        assert len(index.decks) == 1
        deck_id = index.decks[0].id
        assert index.default_id == deck_id
        assert cv_decks.deck_path(settings, deck_id).exists()

        saved = await cv_decks.load_deck(settings, deck_id)
        assert saved is not None
        assert saved.contact.name == "Jane Doe"
        assert index.decks[0].has_cv is True

        assert not settings.cv_structure_path.exists(), (
            "the bootstrap must not dual-write the legacy cv_structure.json"
        )

    async def test_ignores_cv_when_structure_already_exists(self, tmp_path, monkeypatch, capsys):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        await cv_structure.save(settings, CVDocument.model_validate(_VALID_CV))
        cv_path = tmp_path / "resume.pdf"
        cv_path.write_bytes(b"%PDF-1.4")

        # If infer were invoked, this empty-reply FakeAgentBackend would raise on pop.
        monkeypatch.setattr(
            "jsa.cli.make_backend_factory",
            lambda s: (lambda name: FakeAgentBackend([])),
        )

        await _bootstrap_cv_structure(settings, cv_path)

        out = capsys.readouterr().out
        assert "ignored" in out.lower()

    async def test_proceeds_with_no_cv_and_no_structure(self, tmp_path, capsys):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        await _bootstrap_cv_structure(settings, None)
        assert not settings.cv_structure_path.exists()
        out = capsys.readouterr().out
        assert "no cv structure found" in out.lower()

    async def test_infer_error_exits_nonzero(self, tmp_path, monkeypatch):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        cv_path = tmp_path / "resume.pdf"
        cv_path.write_bytes(b"%PDF-1.4")

        async def _raise_infer(*args, **kwargs):
            raise InferError("the inferred structure was not valid JSON")

        monkeypatch.setattr("jsa.cli.run_infer", _raise_infer)
        monkeypatch.setattr(
            "jsa.cli.make_backend_factory",
            lambda s: (lambda name: FakeAgentBackend([_final("not json")])),
        )

        with pytest.raises(typer.Exit) as exc_info:
            await _bootstrap_cv_structure(settings, cv_path)
        assert exc_info.value.exit_code == 1
        assert not settings.cv_structure_path.exists()
