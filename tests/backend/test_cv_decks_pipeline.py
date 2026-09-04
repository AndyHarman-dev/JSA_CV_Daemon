"""Phase 4 — the pipeline seam: a job's `base_cv_id` reaches its prompts.

The assertions here deliberately land on the **captured initial user message** the
backend actually received, never on what `cv_decks.resolve_path` returned. Asserting the
resolver's return value re-tests Phase 1 and would pass even if `Orchestrator._run_one`
never threaded it through — which is precisely the seam this phase adds.

Everything runs through a real `Orchestrator.run()` loop (gate included) rather than
calling `run_stage` directly, for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Job, JobState
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema import CVDocument
from jsa.server import create_app, make_base_cv_resolver
from jsa.store import cv_decks
from tests.backend.fakes.fake_backend import FakeAgentBackend


def _cv(name: str) -> dict:
    return {
        "contact": {"name": name, "email": "x@x.com"},
        "sections": [
            {"name": "Summary", "text": f"{name} is a backend engineer of six years."},
            {"name": "Skills", "items": ["Python", "Go"]},
        ],
    }


def _final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


class CapturingBackend(FakeAgentBackend):
    """Records the initial user message of the first session it is asked to start."""

    def __init__(self, replies: list[AgentReply], sink: list[str]) -> None:
        super().__init__(replies)
        self._sink = sink

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
        structured_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        self._sink.append(initial_user_msg)
        return await super().start_session(
            system_prompt, initial_user_msg, structured_schema, **kwargs
        )


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


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "jsa.sqlite")


async def _insert_job(factory, *, base_cv_id: str | None = None) -> Job:
    async with factory() as s:
        job = await repo.upsert_job(
            s,
            dict(
                id=uuid4().hex[:16],
                company="Acme",
                role="Engineer",
                link="https://acme.com/job",
                tier="A",
                jd="Job description text",
                jd_hash="hash0000deadbeef",
            ),
        )
        job.state = JobState.pending
        job.base_cv_id = base_cv_id
        await s.commit()
    return job


async def _run_until_dispatched(orch: Orchestrator, factory, job_id: str, timeout: float = 5.0):
    """Drive orch.run() until `job_id` leaves pending; return its final row."""
    task = asyncio.create_task(orch.run())
    try:
        await asyncio.sleep(0.1)  # let the loop settle into its gated wait first
        orch.kick()
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            async with factory() as s:
                refreshed = await repo.get_job(s, job_id)
            if refreshed.state != JobState.pending:
                return refreshed
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"job never left pending; state={refreshed.state}")
            await asyncio.sleep(0.05)
    finally:
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(task, timeout=5.0)


async def _dispatch_and_capture(settings, session_factory, *, base_cv_id: str | None) -> str:
    """Run one job through the fit_assessment stage; return the message the backend saw."""
    sink: list[str] = []
    job = await _insert_job(session_factory, base_cv_id=base_cv_id)
    orch = Orchestrator(
        db_session_factory=session_factory,
        backend_factory=lambda name: CapturingBackend([_final("UNFIT\nNot a match.")], sink),
        base_cv_resolver=make_base_cv_resolver(settings),
    )
    await _run_until_dispatched(orch, session_factory, job.id)
    assert sink, "the backend was never asked to start a session"
    return sink[0]


class TestPerJobDeckReachesThePrompt:
    """Two decks with distinguishable contact names; the job's assignment decides which
    one's content the model is actually shown."""

    @pytest.fixture
    async def two_decks(self, settings):
        a = await cv_decks.create_deck(settings, name="deck-a")
        await cv_decks.save_deck(settings, a.id, CVDocument.model_validate(_cv("Alice Anderson")))
        b = await cv_decks.create_deck(settings, name="deck-b")
        await cv_decks.save_deck(settings, b.id, CVDocument.model_validate(_cv("Bob Brown")))
        # deck A is the default (created first)
        index = await cv_decks.load_index(settings)
        assert index.default_id == a.id
        return a.id, b.id

    async def test_assigned_deck_content_is_what_the_model_sees(
        self, settings, session_factory, two_decks
    ):
        _a_id, b_id = two_decks
        msg = await _dispatch_and_capture(settings, session_factory, base_cv_id=b_id)
        assert "Bob Brown" in msg
        assert "Alice Anderson" not in msg

    async def test_unassigned_job_gets_the_default_deck(
        self, settings, session_factory, two_decks
    ):
        msg = await _dispatch_and_capture(settings, session_factory, base_cv_id=None)
        assert "Alice Anderson" in msg
        assert "Bob Brown" not in msg

    async def test_unknown_deck_id_falls_back_to_default_and_warns(
        self, settings, session_factory, two_decks, caplog
    ):
        """A deck deleted mid-flight (or a stale id) must never fail the job — it falls
        back to the default, loudly."""
        with caplog.at_level(logging.WARNING, logger="jsa.store.cv_decks"):
            msg = await _dispatch_and_capture(
                settings, session_factory, base_cv_id="0" * 32
            )
        assert "Alice Anderson" in msg
        assert any("falling back to deck" in r.getMessage() for r in caplog.records)

    async def test_malformed_deck_id_falls_back_and_never_fails_the_job(
        self, settings, session_factory, two_decks
    ):
        """`resolve_path` skips an id that fails the path-traversal guard rather than
        raising it into the dispatch loop, which would kill the orchestrator."""
        msg = await _dispatch_and_capture(settings, session_factory, base_cv_id="../etc/passwd")
        assert "Alice Anderson" in msg


class TestFitBackendSeesTheSameDeck:
    """`--fit-model` builds a *separate* backend for the fit stage. It is handed the same
    resolved path — a pinned fit model must not silently read a different base CV."""

    async def test_fit_backend_receives_the_assigned_deck(self, settings, session_factory):
        a = await cv_decks.create_deck(settings, name="deck-a")
        await cv_decks.save_deck(settings, a.id, CVDocument.model_validate(_cv("Alice Anderson")))
        b = await cv_decks.create_deck(settings, name="deck-b")
        await cv_decks.save_deck(settings, b.id, CVDocument.model_validate(_cv("Bob Brown")))

        general_sink: list[str] = []
        fit_sink: list[str] = []
        job = await _insert_job(session_factory, base_cv_id=b.id)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: CapturingBackend([_final("FIT")], general_sink),
            fit_backend_factory=lambda name, model=None: CapturingBackend(
                [_final("UNFIT\nNot a match.")], fit_sink
            ),
            base_cv_resolver=make_base_cv_resolver(settings),
        )
        await _run_until_dispatched(orch, session_factory, job.id)

        assert fit_sink, "the fit backend was never used"
        assert "Bob Brown" in fit_sink[0]
        assert "Alice Anderson" not in fit_sink[0]


class TestGateAcrossTheEditorSeam:
    """The gate must be answered by the same store the editor writes to. Before Phase 4
    the editor wrote a deck while the orchestrator read the legacy `cv_structure.json`,
    so a first save left every job `pending` forever with the banner already cleared."""

    async def test_zero_decks_blocks_and_an_editor_save_unblocks(
        self, settings, session_factory
    ):
        app = create_app(settings)
        app.state.session_factory = session_factory

        sink: list[str] = []
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: CapturingBackend([_final("UNFIT\nNot a match.")], sink),
            base_cv_resolver=make_base_cv_resolver(settings),
        )
        app.state.orchestrator = orch
        job = await _insert_job(session_factory)

        task = asyncio.create_task(orch.run())
        try:
            await asyncio.sleep(0.1)
            orch.kick()
            await asyncio.sleep(0.2)

            async with session_factory() as s:
                refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.pending, "gate must block with zero decks"
            assert sink == []

            # Save through the real editor route — not save_deck() — so this covers the
            # whole editor→gate seam, which is where the two stores used to disagree.
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.put("/api/cv-structure", json={"structured": _cv("Alice Anderson")})
            assert resp.status_code == 200, resp.text

            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                async with session_factory() as s:
                    refreshed = await repo.get_job(s, job.id)
                if refreshed.state != JobState.pending:
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError("editor save never unblocked the gate")
                await asyncio.sleep(0.05)
            assert "Alice Anderson" in sink[0]
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_config_banner_agrees_with_the_gate_after_the_save(
        self, settings, session_factory
    ):
        """Review finding 2: `/api/config`'s `cv_structure_exists` and the gate must not
        disagree about whether a base CV is available."""
        app = create_app(settings)
        app.state.session_factory = session_factory
        app.state.orchestrator = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([_final("FIT")]),
            base_cv_resolver=make_base_cv_resolver(settings),
        )
        resolver = make_base_cv_resolver(settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before = await client.get("/api/config")
            assert before.json()["cv_structure_exists"] is False
            assert await resolver(None) is None

            resp = await client.put("/api/cv-structure", json={"structured": _cv("Alice Anderson")})
            assert resp.status_code == 200, resp.text

            after = await client.get("/api/config")
            assert after.json()["cv_structure_exists"] is True
            assert after.json()["cv_deck_count"] == 1
            assert await resolver(None) is not None
