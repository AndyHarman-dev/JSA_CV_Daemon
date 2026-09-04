"""Unit tests for jsa.pipeline.orchestrator — Orchestrator class.

Uses FakeAgentBackend (scripted replies) and in-memory SQLite.
All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle
from jsa.agents.google_cli import GoogleCliSessionExpiredError
from jsa.db import repo
from jsa.db.models import (
    Base,
    FollowUp,
    Job,
    JobState,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.orchestrator import Orchestrator, _next_stage_for
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cl_final, cv_final, fit_reply


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
    """Insert a fresh job and launch it (queued → pending) so it's runnable."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.pending
        await s.commit()
    return job


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


async def _approve_cv(factory, job_id: str, orch: Orchestrator) -> None:
    """Simulate the approve-cv action (Phase 4's POST /api/jobs/{id}/approve-cv route
    body): cv_review → cv_done, then kick the orchestrator so cover_letter dispatches."""
    async with factory() as s:
        job = await repo.get_job(s, job_id)
        await repo.checkpoint(s, job, JobState.cv_done, None)
    orch.kick()


async def _poll_job_state(
    factory,
    job_id: str,
    target_state: JobState,
    timeout: float = 5.0,
    orch: Orchestrator | None = None,
    settled: Callable[[Job], bool] | None = None,
) -> Job:
    """Poll the DB until job reaches target_state or timeout expires.

    When target_state is 'review' and orch is given, a job that parks at the CV gate
    (cv_review) is auto-approved (see _approve_cv) so the cover-letter lane starts — the
    two-lane pipeline no longer advances a job past the CV gate on its own.

    ``settled`` is an optional extra condition the row must ALSO satisfy before the state
    counts as reached. It exists because a bare state match is not always a terminal
    match: a handler that writes across two transactions makes its intermediate row
    durably readable between them, so a poll on the state alone can return on a row the
    handler is still about to change (``_handle_session_expired`` commits
    ``failed``/``retry_count=0`` before committing the ``soft_reset_job`` that follows).
    It narrows *when the wait ends*, never what a test then asserts.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    approved = False
    while True:
        async with factory() as s:
            job = await repo.get_job(s, job_id)
        if job is not None and job.state == target_state and (settled is None or settled(job)):
            return job
        if (
            not approved
            and orch is not None
            and target_state == JobState.review
            and job is not None
            and job.state == JobState.cv_review
        ):
            approved = True
            await _approve_cv(factory, job_id, orch)
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach {target_state} within {timeout}s "
                f"(current state: {job.state if job else 'None'})"
            )
        await asyncio.sleep(0.05)


async def _drain_inflight(orch: Orchestrator, timeout: float = 5.0) -> None:
    """Wait for the per-job tasks ``Orchestrator.run()`` spawned but never awaits.

    ``run()`` is ``while not self._stopping: ... await self.wakeup.wait()`` — it returns
    as soon as it sees ``_stopping`` and does **not** await ``self._tasks``. So a
    ``_run_one`` can still be mid-flight after the run task has been awaited, which bites
    two ways: (a) it keeps writing to the DB, and ``_handle_session_expired`` in
    particular commits its intermediate ``failed``/``retry_count=0`` state in a *separate*
    transaction from the ``soft_reset_job`` that follows, so a test that stopped on
    ``failed`` can read the row between the two commits; (b) the leaked task survives into
    a later test, where it can consume a module-global monkeypatch or a one-shot fixture
    flag that test set up for itself.

    Wait for them to finish, then cancel and reap anything still parked.
    """
    inflight = [t for t in list(orch._tasks.values()) if not t.done()]
    if not inflight:
        return
    _, pending = await asyncio.wait(inflight, timeout=timeout)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_orchestrator_until(
    orch: Orchestrator,
    factory,
    job_ids: list[str],
    target_state: JobState,
    timeout: float = 10.0,
    settled: Callable[[Job], bool] | None = None,
) -> None:
    """Run the orchestrator as a task, wait for all jobs to reach target_state, then stop."""
    task = asyncio.create_task(orch.run())
    try:
        await asyncio.gather(
            *[
                asyncio.wait_for(
                    _poll_job_state(factory, jid, target_state, orch=orch, settled=settled),
                    timeout=timeout,
                )
                for jid in job_ids
            ]
        )
    finally:
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(task, timeout=5.0)
        await _drain_inflight(orch)


# ---------------------------------------------------------------------------
# Test: _next_stage_for state → stage mapping
# ---------------------------------------------------------------------------


class _StubJob:
    def __init__(self, state, current_stage=None):
        self.state = state
        self.current_stage = current_stage
        self.id = "stub-job"


class TestNextStageFor:
    def test_pending_returns_fit_assessment(self):
        assert _next_stage_for(_StubJob(JobState.pending)) == Stage.fit_assessment

    def test_fit_done_returns_cv_adjust(self):
        assert _next_stage_for(_StubJob(JobState.fit_done)) == Stage.cv_adjust

    def test_cv_done_returns_cover_letter(self):
        assert _next_stage_for(_StubJob(JobState.cv_done)) == Stage.cover_letter

    def test_cv_review_returns_current_stage(self):
        job = _StubJob(JobState.cv_review, current_stage=Stage.revising_cv)
        assert _next_stage_for(job) == Stage.revising_cv

    def test_cv_review_with_none_stage_raises(self):
        job = _StubJob(JobState.cv_review, current_stage=None)
        with pytest.raises(ValueError):
            _next_stage_for(job)

    def test_review_returns_current_stage(self):
        job = _StubJob(JobState.review, current_stage=Stage.revising_cl)
        assert _next_stage_for(job) == Stage.revising_cl

    def test_awaiting_input_returns_current_stage(self):
        job = _StubJob(JobState.awaiting_input, current_stage=Stage.cover_letter)
        assert _next_stage_for(job) == Stage.cover_letter


# ---------------------------------------------------------------------------
# Test: Picks up pending job and completes cv_adjust
# ---------------------------------------------------------------------------


class TestPicksUpPendingJob:
    async def test_pending_job_completes_pipeline_to_review(self, session_factory):
        """Pending job passes fit_assessment → cv_adjust, parks at the cv_review gate,
        gets auto-approved (see _poll_job_state / _approve_cv — the two-lane pipeline no
        longer advances a job past the CV gate on its own), then cover_letter → review.
        We wait for the final resting state (review) and then verify a cv_adjust Document
        exists (proving the CV gate was passed through).
        """
        job = await _insert_job(session_factory)

        # A shared backend instance draws one reply per stage in order: fit_assessment,
        # then cv_adjust, then cover_letter (fit_backend_factory=None → same backend).
        backend = FakeAgentBackend([fit_reply(), cv_final(), cl_final()])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
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

        backend = FakeAgentBackend([fit_reply(), cv_final(), cl_final()])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
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
                super().__init__([cv_final()])

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
        await _drain_inflight(orch)


# ---------------------------------------------------------------------------
# Test: kick() unblocks the loop
# ---------------------------------------------------------------------------


class TestKickUnblocksLoop:
    async def test_kick_causes_newly_runnable_job_to_be_processed(self, session_factory):
        """Orchestrator is waiting; after kick() is called with a new job, it processes it."""
        # A shared backend instance draws one reply per stage in order: fit_assessment,
        # then cv_adjust, then cover_letter, before resting at review.
        backend = FakeAgentBackend([fit_reply(), cv_final(), cl_final()])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
        )
        orch_task = asyncio.create_task(orch.run())

        # Let orchestrator start and enter its wakeup.wait() with no runnable jobs
        await asyncio.sleep(0.1)

        # Insert a job and kick the orchestrator
        job = await _insert_job(session_factory)
        orch.kick()

        # Wait for the full pipeline to reach review
        await asyncio.wait_for(
            _poll_job_state(session_factory, job.id, JobState.review, orch=orch),
            timeout=5.0,
        )

        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(orch_task, timeout=5.0)
        await _drain_inflight(orch)

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

        # fit_assessment runs first (FIT), then cv_adjust parks on NEED_INPUT.
        backend = FakeAgentBackend([fit_reply(), _needs_input_reply("What is your role?")])
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

        # fit_assessment runs first (FIT), then cv_adjust parks on NEED_INPUT.
        backend = FakeAgentBackend([fit_reply(), _needs_input_reply("What is your role?")])
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

        backend_park = FakeAgentBackend([fit_reply(), _needs_input_reply("What is your target role?")])
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

        # Step 3: Run a new orchestrator. restore_session pops nothing, so the resume
        # stage (cv_adjust) consumes the first scripted reply; cover_letter the second.
        backend_resume = FakeAgentBackend([cv_final(), cl_final()])
        orch_resume = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_resume,
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

        backend_park = FakeAgentBackend([fit_reply(), _needs_input_reply("What is your availability?")])
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


# ---------------------------------------------------------------------------
# Test: Gemini session-expiry auto-recovery
# ---------------------------------------------------------------------------


class TestGoogleSessionExpiredAutoRecovery:
    async def test_auto_resets_once_then_fails_permanently(self, session_factory):
        """GoogleCliSessionExpiredError triggers one auto soft-reset; second expiry → failed.

        A backend that always raises GoogleCliSessionExpiredError will cause:
        - First run: job is pending (retry_count=0) → auto soft-reset → pending (retry_count=1)
        - Second run: retry_count=1 > 0 → no further reset → job stays failed
        """
        call_count = [0]

        class AlwaysExpiresBackend(FakeAgentBackend):
            def __init__(self):
                super().__init__([])

            async def start_session(self, system_prompt, initial_user_msg):
                call_count[0] += 1
                raise GoogleCliSessionExpiredError(
                    f"Gemini session not found (test call #{call_count[0]})"
                )

        job = await _insert_job(session_factory)
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=AlwaysExpiresBackend,
        )

        # `failed` alone is ambiguous here: _handle_session_expired commits
        # failed/retry_count=0 in one transaction and the soft-reset in the NEXT one, so
        # the FIRST expiry makes a `failed` row briefly readable on its way to `pending`.
        # The terminal failure is the one where the auto-recovery has already been spent.
        await _run_orchestrator_until(
            orch,
            session_factory,
            [job.id],
            JobState.failed,
            settled=lambda j: j.retry_count >= 1,
        )

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)

        assert refreshed.state == JobState.failed
        # retry_count==1 proves the auto-recovery ran exactly once before giving up
        assert refreshed.retry_count == 1
        assert call_count[0] == 2

    async def test_no_auto_reset_when_already_retried(self, session_factory):
        """If retry_count is already >0, a GoogleCliSessionExpiredError marks the job failed immediately."""
        job = await _insert_job(session_factory)

        # Manually bump retry_count to simulate a previously auto-recovered job
        async with session_factory() as s:
            j = await repo.get_job(s, job.id)
            j.retry_count = 1
            await s.commit()

        class AlwaysExpiresBackend(FakeAgentBackend):
            def __init__(self):
                super().__init__([])

            async def start_session(self, system_prompt, initial_user_msg):
                raise GoogleCliSessionExpiredError("Gemini session not found (test)")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=AlwaysExpiresBackend,
        )

        await _run_orchestrator_until(orch, session_factory, [job.id], JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)

        assert refreshed.state == JobState.failed
        assert "session not found" in (refreshed.error or "").lower()
