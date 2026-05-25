"""Unit tests for jsa.pipeline.orchestrator — Orchestrator class.

Uses FakeAgentBackend (scripted replies) and in-memory SQLite.
All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle
from jsa.db import repo
from jsa.db.models import (
    Base,
    FollowUp,
    Job,
    JobState,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.orchestrator import Orchestrator
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory():
    """In-memory SQLite with StaticPool so all sessions share the same DB."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_data(
    job_id: str | None = None,
    company: str = "Acme",
    role: str = "Engineer",
    link: str = "https://acme.com/job",
    tier: str = "A",
    jd: str = "Job description text",
    jd_hash: str = "hash0000deadbeef",
    cv_text: str = "Curriculum vitae text",
) -> dict:
    if job_id is None:
        job_id = uuid4().hex[:16]
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


async def _insert_job(factory, **overrides) -> Job:
    """Insert a fresh pending job."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        await s.commit()
    return job


def _final_reply(content: str = "# Document\nContent here.") -> AgentReply:
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


async def _poll_job_state(
    factory,
    job_id: str,
    target_state: JobState,
    timeout: float = 5.0,
) -> Job:
    """Poll the DB until job reaches target_state or timeout expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with factory() as s:
            job = await repo.get_job(s, job_id)
        if job is not None and job.state == target_state:
            return job
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach {target_state} within {timeout}s "
                f"(current state: {job.state if job else 'None'})"
            )
        await asyncio.sleep(0.05)


async def _run_orchestrator_until(
    orch: Orchestrator,
    factory,
    job_ids: list[str],
    target_state: JobState,
    timeout: float = 10.0,
) -> None:
    """Run the orchestrator as a task, wait for all jobs to reach target_state, then stop."""
    task = asyncio.create_task(orch.run())
    try:
        await asyncio.gather(
            *[
                asyncio.wait_for(
                    _poll_job_state(factory, jid, target_state),
                    timeout=timeout,
                )
                for jid in job_ids
            ]
        )
    finally:
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Test: Picks up pending job and completes cv_adjust
# ---------------------------------------------------------------------------


class TestPicksUpPendingJob:
    async def test_pending_job_completes_pipeline_to_review(self, session_factory):
        """Pending job passes through cv_adjust (cv_done) then cover_letter to reach review.

        cv_done is transient — the orchestrator immediately picks the job up
        for cover_letter. We wait for the final resting state (review) and then
        verify that a cv_adjust Document exists (proving cv_done was passed through).
        """
        job = await _insert_job(session_factory)

        orch = Orchestrator(
            db_session_factory=session_factory,
            # Each factory call produces a fresh backend with one reply.
            backend_factory=lambda: FakeAgentBackend([_final_reply("# Stage output")]),
        )

        # Wait for the full pipeline to reach review.
        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.review)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
            # Verify final state
            assert refreshed.state == JobState.review
            # Verify the cv_adjust Document was written (proves cv_done was passed through)
            cv_docs = await repo.get_documents(s, job.id, stage=Stage.cv_adjust)
            assert len(cv_docs) == 1
            cl_docs = await repo.get_documents(s, job.id, stage=Stage.cover_letter)
            assert len(cl_docs) == 1

    async def test_two_stage_pipeline_completes_to_review(self, session_factory):
        """Pending job goes through cv_adjust then cover_letter and reaches review."""
        job = await _insert_job(session_factory)

        # Each backend factory call gets one fresh reply (one per stage).
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([_final_reply("# Stage output")]),
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.review)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.review


# ---------------------------------------------------------------------------
# Test: Semaphore limits concurrency to max_parallel
# ---------------------------------------------------------------------------


class TestSemaphoreConcurrencyLimit:
    async def test_at_most_max_parallel_jobs_running_simultaneously(self, session_factory):
        """With 7 pending jobs and max_parallel=3, at most 3 are running at once.

        Note: This test emits a benign SAWarning about SQLAlchemy identity map collisions.
        This is a known artifact of StaticPool + concurrent writes from multiple workers
        sharing the same underlying connection. It does not affect correctness.
        """
        MAX_PARALLEL = 3
        NUM_JOBS = 7

        # Event to hold backends inside start_session until we release them
        gate = asyncio.Event()
        # Counter tracking concurrent running workers
        concurrent_peak = [0]
        concurrent_current = [0]

        class BlockingBackend(FakeAgentBackend):
            def __init__(self):
                # One reply per backend instance (called once)
                super().__init__([_final_reply("# CV")])

            async def start_session(self, system_prompt, initial_user_msg):
                concurrent_current[0] += 1
                concurrent_peak[0] = max(concurrent_peak[0], concurrent_current[0])
                # Block until gate is set
                await gate.wait()
                concurrent_current[0] -= 1
                handle = FakeSessionHandle(id=str(uuid4()), external_id=None)
                reply = self._pop_reply()
                return handle, reply

        # Insert 7 pending jobs
        job_ids = []
        for i in range(NUM_JOBS):
            job = await _insert_job(session_factory, job_id=f"job{i:012d}aaaa")
            job_ids.append(job.id)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=BlockingBackend,
            max_parallel=MAX_PARALLEL,
        )

        # Run the orchestrator briefly — it will fill the semaphore and block
        orch_task = asyncio.create_task(orch.run())

        # Wait until max_parallel workers are blocked inside start_session
        deadline = asyncio.get_event_loop().time() + 5.0
        while concurrent_current[0] < MAX_PARALLEL:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.02)

        # The peak should not exceed MAX_PARALLEL
        assert concurrent_peak[0] <= MAX_PARALLEL, (
            f"Peak concurrency {concurrent_peak[0]} exceeded limit {MAX_PARALLEL}"
        )

        # Release all and stop
        gate.set()
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(orch_task, timeout=10.0)


# ---------------------------------------------------------------------------
# Test: kick() unblocks the loop
# ---------------------------------------------------------------------------


class TestKickUnblocksLoop:
    async def test_kick_causes_newly_runnable_job_to_be_processed(self, session_factory):
        """Orchestrator is waiting; after kick() is called with a new job, it processes it."""
        # Each factory call creates a fresh backend with one reply. The pipeline
        # runs two stages (cv_adjust, cover_letter) before resting at review.
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([_final_reply("# Output")]),
        )
        orch_task = asyncio.create_task(orch.run())

        # Let orchestrator start and enter its wakeup.wait() with no runnable jobs
        await asyncio.sleep(0.1)

        # Insert a job and kick the orchestrator
        job = await _insert_job(session_factory)
        orch.kick()

        # Wait for the full pipeline to reach review
        await asyncio.wait_for(
            _poll_job_state(session_factory, job.id, JobState.review),
            timeout=5.0,
        )

        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(orch_task, timeout=5.0)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.review


# ---------------------------------------------------------------------------
# Test: PausedForInput handled silently (job → awaiting_input, not failed)
# ---------------------------------------------------------------------------


class TestPausedForInputSilent:
    async def test_needs_input_does_not_mark_job_failed(self, session_factory):
        """When agent returns needs_input, job goes to awaiting_input — not failed."""
        job = await _insert_job(session_factory)

        backend = FakeAgentBackend([_needs_input_reply("What is your role?")])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
        )

        await _run_orchestrator_until(
            orch, session_factory, [job.id], JobState.awaiting_input
        )

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.awaiting_input
        assert refreshed.state != JobState.failed

    async def test_follow_up_row_present_after_parking(self, session_factory):
        """FollowUp row is inserted when job parks to awaiting_input."""
        job = await _insert_job(session_factory)

        backend = FakeAgentBackend([_needs_input_reply("What is your role?")])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
        )

        await _run_orchestrator_until(
            orch, session_factory, [job.id], JobState.awaiting_input
        )

        async with session_factory() as s:
            fus = await repo.get_follow_ups(s, job.id, answered=False)
        assert len(fus) == 1
        assert fus[0].question == "What is your role?"


# ---------------------------------------------------------------------------
# Test: Exception → mark job failed
# ---------------------------------------------------------------------------


class TestExceptionMarksJobFailed:
    async def test_unexpected_exception_marks_job_failed(self, session_factory):
        """If the agent raises an unexpected exception, the job is marked failed."""

        class BrokenBackend(FakeAgentBackend):
            def __init__(self):
                super().__init__([])

            async def start_session(self, system_prompt, initial_user_msg):
                raise RuntimeError("Agent exploded unexpectedly")

        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=BrokenBackend,
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed
        assert "Agent exploded" in (refreshed.error or "")

    async def test_error_message_stored_on_job(self, session_factory):
        """The exception message is stored in job.error."""
        ERROR_MSG = "Specific error for test"

        class BrokenBackend(FakeAgentBackend):
            def __init__(self):
                super().__init__([])

            async def start_session(self, system_prompt, initial_user_msg):
                raise ValueError(ERROR_MSG)

        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=BrokenBackend,
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert ERROR_MSG in (refreshed.error or "")


# ---------------------------------------------------------------------------
# Test: awaiting_input resume — orchestrator picks up answered job
# ---------------------------------------------------------------------------


class TestAwaitingInputResume:
    async def test_answered_followup_job_completes(self, session_factory):
        """A job parked in awaiting_input with an answered FollowUp resumes to review."""
        # Step 1: Run the job until it parks on cv_adjust
        job = await _insert_job(session_factory)

        backend_park = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        orch_park = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_park,
        )
        await _run_orchestrator_until(
            orch_park, session_factory, [job.id], JobState.awaiting_input
        )

        # Step 2: Simulate user answering the follow-up
        async with session_factory() as s:
            fus = await repo.get_follow_ups(s, job.id, answered=False)
            assert len(fus) == 1
            fu = fus[0]
            fu.answer = "Software Engineer"
            fu.answered_at = datetime.utcnow()
            await s.commit()

        # Step 3: Run a new orchestrator. The resume stage (cv_adjust) uses
        # one reply; the subsequent cover_letter stage uses another. Use a
        # factory that creates fresh 1-reply backends so the pipeline never
        # runs out of replies.
        orch_resume = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([_final_reply("# Stage output")]),
        )
        # Kick immediately so it picks up the answered job
        orch_resume.kick()

        # Wait for the full pipeline to reach review
        await _run_orchestrator_until(
            orch_resume, session_factory, [job.id], JobState.review
        )

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.review

    async def test_unanswered_followup_stays_parked(self, session_factory):
        """A job in awaiting_input with unanswered FollowUp is NOT dispatched."""
        job = await _insert_job(session_factory)

        backend_park = FakeAgentBackend([_needs_input_reply("What is your availability?")])
        orch_park = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_park,
        )
        await _run_orchestrator_until(
            orch_park, session_factory, [job.id], JobState.awaiting_input
        )

        # DO NOT answer the follow-up

        # Run another orchestrator — it should not pick up the job
        backend_probe = FakeAgentBackend([])  # no replies — would error if called
        orch_probe = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_probe,
        )
        orch_probe_task = asyncio.create_task(orch_probe.run())
        # Give the orchestrator time to run a dispatch cycle
        await asyncio.sleep(0.2)
        orch_probe._stopping = True
        orch_probe.kick()
        await asyncio.wait_for(orch_probe_task, timeout=5.0)

        # Job should still be awaiting_input
        async with session_factory() as s:
            still_parked = await repo.get_job(s, job.id)
        assert still_parked.state == JobState.awaiting_input
