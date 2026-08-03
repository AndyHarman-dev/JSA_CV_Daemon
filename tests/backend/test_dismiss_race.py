"""Regression tests for the dismiss/cancel resurrection race.

Bug: 5 jobs run in parallel. Dismissing a job before its agent turn returns
marks it `dismissed`, but the orchestrator worker holds an in-memory Job
object (state=running) for the whole agent call. When the agent finally
replies, the worker's checkpoint validates only against that stale in-memory
state and overwrites `dismissed` back to `awaiting_input` (or a FINAL state),
resurrecting the job with a NEED_INPUT prompt as if it were never dismissed.

Fix: jsa.pipeline.stages.run_stage re-reads the job's state from a FRESH
session (jsa.db.repo.get_state_fresh) right before dispatching the reply. If
the job is no longer `running`, it raises StaleJobResult and discards the
result instead of checkpointing it. jsa.pipeline.orchestrator additionally
lets dismiss cancel the in-flight worker task outright (best-effort; the
fresh-state guard is the correctness backstop either way).

Uses FakeAgentBackend and in-memory SQLite (StaticPool) per project
convention — see tests/backend/fakes/fake_backend.py and test_orchestrator.py.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, Document, FollowUp, JobState, Stage
from jsa.pipeline import stages
from jsa.pipeline.orchestrator import Orchestrator
from tests.backend.fakes.fake_backend import FakeAgentBackend


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/backend/test_orchestrator.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    """In-memory SQLite with StaticPool so all sessions share the same DB —
    required to simulate two concurrent sessions (worker vs. dismiss route)."""
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


def _job_data(job_id: str | None = None) -> dict:
    return dict(
        id=job_id or uuid4().hex[:16],
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
        cv_text="Curriculum vitae text",
    )


async def _insert_running_job(factory, stage: Stage = Stage.cv_adjust):
    """Insert a job and put it directly into running(stage), as the
    orchestrator's dispatch loop would before spawning a worker
    (orchestrator.py: transition(db_job, JobState.running, stage))."""
    async with factory() as s:
        job = await repo.upsert_job(s, _job_data())
        job.state = JobState.pending  # simulate LAUNCH (queued → pending) before dispatch
        await repo.checkpoint(s, job, JobState.running, stage)
        await s.commit()
    return job


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


def _final_reply(content: str = "# Document\nContent here.") -> AgentReply:
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _fit_reply() -> AgentReply:
    """A passing fit-assessment verdict (first line FIT)."""
    return AgentReply(raw="<<<FINAL>>>\nFIT\n<<<END>>>", content="FIT", kind="final")


def _cv_final_reply() -> AgentReply:
    """A schema-valid cv_adjust FINAL payload (structured JSON CVDocument) —
    plain markdown fails jsa.pipeline.stages._validate_final_content's JSON
    schema gate before ever reaching the code under test here."""
    content = json.dumps(
        {
            "contact": {"name": "Jane Doe"},
            "sections": [
                {"name": "Summary", "text": "Experienced engineer."},
            ],
        }
    )
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


# ---------------------------------------------------------------------------
# Test 1: the reported bug — stale NEED_INPUT must not resurrect a dismissed job
# ---------------------------------------------------------------------------


class TestStaleNeedsInputDiscarded:
    async def test_dismiss_during_agent_call_wins_over_stale_needs_input(
        self, session_factory
    ):
        """Faithful two-session repro of the race:

        Session A: get_job for a running job (mirrors orchestrator.py _run_one's
                    single get_job before the long agent call).
        Session B: a SEPARATE session commits dismissed (mirrors the dismiss route).
        Session A: drives run_stage to completion; the agent returns needs_input.

        Without the fix, run_stage would checkpoint awaiting_input on session A's
        stale in-memory `running` job, overwriting `dismissed` and creating a
        FollowUp — reproducing the reported bug. With the fix, run_stage must
        raise StaleJobResult and write nothing.
        """
        job = await _insert_running_job(session_factory, Stage.cv_adjust)

        # Session A: same pattern as orchestrator.py:215 — one get_job, held
        # in memory for the (simulated) duration of the agent call below.
        session_a = session_factory()
        try:
            job_a = await repo.get_job(session_a, job.id)
            assert job_a.state == JobState.running

            # Session B: separate session, commits the concurrent dismiss.
            async with session_factory() as session_b:
                job_b = await repo.get_job(session_b, job.id)
                await repo.checkpoint(session_b, job_b, JobState.dismissed, new_stage=None)

            # Sanity: the dismiss is durably committed and visible to a fresh read.
            assert await repo.get_state_fresh(session_a, job.id) == JobState.dismissed

            backend = FakeAgentBackend([_needs_input_reply("What is your role?")])

            with pytest.raises(stages.StaleJobResult):
                await stages.run_stage(job_a, backend, Stage.cv_adjust, session_a)
        finally:
            await session_a.close()

        # The dismissed state must survive untouched.
        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.dismissed
            fus = await repo.get_follow_ups(s, job.id, answered=False)
            assert fus == []

    async def test_dismiss_during_agent_call_wins_over_stale_final(
        self, session_factory
    ):
        """Same race, but the agent returns FINAL instead of needs_input — the
        guard must protect the FINAL path too (not just NEED_INPUT)."""
        job = await _insert_running_job(session_factory, Stage.cv_adjust)

        session_a = session_factory()
        try:
            job_a = await repo.get_job(session_a, job.id)

            async with session_factory() as session_b:
                job_b = await repo.get_job(session_b, job.id)
                await repo.checkpoint(session_b, job_b, JobState.dismissed, new_stage=None)

            backend = FakeAgentBackend([_cv_final_reply()])

            with pytest.raises(stages.StaleJobResult):
                await stages.run_stage(job_a, backend, Stage.cv_adjust, session_a)
        finally:
            await session_a.close()

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.dismissed
            docs = await repo.get_documents(s, job.id, stage=Stage.cv_adjust)
            assert docs == []


class TestStaleFitAssessmentDiscarded:
    async def test_dismiss_during_fit_assessment_wins_over_stale_fit_done(
        self, session_factory
    ):
        """fit_assessment is the FIRST stage a fresh job runs — exactly the
        "before they output anything" window from the reported bug. It has its
        own checkpoint call sites in _run_fit_assessment (separate from the
        guard in run_stage's main body, which sits after this stage's early
        return), so it needs its own fresh-state check. Without it, a dismiss
        landing during fit_assessment gets silently overwritten to fit_done,
        the job becomes runnable again, and cv_adjust's NEED_INPUT reappears
        downstream — reproducing the bug one stage earlier than the cv_adjust
        race covered above."""
        job = await _insert_running_job(session_factory, Stage.fit_assessment)

        session_a = session_factory()
        try:
            job_a = await repo.get_job(session_a, job.id)
            assert job_a.state == JobState.running

            async with session_factory() as session_b:
                job_b = await repo.get_job(session_b, job.id)
                await repo.checkpoint(session_b, job_b, JobState.dismissed, new_stage=None)

            backend = FakeAgentBackend([_fit_reply()])

            with pytest.raises(stages.StaleJobResult):
                await stages.run_stage(job_a, backend, Stage.fit_assessment, session_a, fit_assessment_backend=backend)
        finally:
            await session_a.close()

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
            assert refreshed.state == JobState.dismissed
            assert refreshed.fit_reason is None


# ---------------------------------------------------------------------------
# Test 2: cancel_task() stops the in-flight worker without marking it failed
# ---------------------------------------------------------------------------


class TestCancelTask:
    async def test_cancel_task_stops_worker_without_marking_failed(
        self, session_factory
    ):
        """cancel_task(job_id) cancels the tracked worker task; _run_one must
        treat the resulting CancelledError as benign, not call mark_failed."""
        job = await _insert_running_job(session_factory, Stage.cv_adjust)

        block_event = asyncio.Event()

        class ForeverBlockingBackend(FakeAgentBackend):
            async def start_session(self, system_prompt, initial_user_msg):
                await block_event.wait()  # never set — blocks until cancelled
                return await super().start_session(system_prompt, initial_user_msg)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: ForeverBlockingBackend([_final_reply()]),
        )

        # Mirrors orchestrator.py's dispatch loop: spawn _run_one and register
        # it in _tasks keyed by job_id (normally done inside run()).
        task = asyncio.create_task(orch._run_one(job.id))
        orch._tasks[job.id] = task
        await asyncio.sleep(0.05)  # let it enter start_session and block

        assert orch.cancel_task(job.id) is True

        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

        # Unrelated job_id: nothing to cancel.
        assert orch.cancel_task("no-such-job") is False

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state != JobState.failed
        assert refreshed.error is None
