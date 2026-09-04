"""End-to-end integration tests using in-memory SQLite + FakeAgentBackend.

These tests use FakeAgentBackend and FakeRenderer — no real API calls are made.
They run as part of the default test suite (pytest -v) since they are deterministic.

Scenarios:
1. Happy path: 2 jobs go pending → cv_review (CV gate, auto-approved by the test
   helper) → cv_done → cover_letter → review → approved
2. Park & resume: job parks at awaiting_input, user answers, job resumes
3. Crash recovery: recovery_sweep corrects running → last checkpoint
4. Revision flow: job in review gets a revision, new document version written
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import (
    Base,
    Document,
    FollowUp,
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.orchestrator import Orchestrator
from jsa.pipeline.state_machine import set_current_stage
from jsa.prompts import loader
from jsa.server import create_app
from tests.backend.fakes.fake_backend import FakeAgentBackend
from tests.backend.fakes.fake_renderer import FakeRenderer
from tests.backend.fakes.finals import cl_final, cv_final, fit_reply


# ---------------------------------------------------------------------------
# Shared fixtures
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
# Helpers (mirror test_orchestrator.py patterns)
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
    """Insert a fresh job, launch it (queued → pending), and return it."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.pending
        await s.commit()
    # Return a detached copy
    async with factory() as s:
        return await repo.get_job(s, data["id"])


def _needs_input_reply(question: str = "What sector?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


class _StageAwareDocBackend(FakeAgentBackend):
    """The orchestrator builds a fresh backend per (job, stage) dispatch, so a positional
    reply list cannot serve cv_adjust and cover_letter at once. Pick from the system prompt."""

    def __init__(self) -> None:
        super().__init__([])

    async def start_session(self, system_prompt, initial_user_msg):
        is_cl = system_prompt.startswith(loader.read_prompt("cover_letter"))
        self._replies = [cl_final() if is_cl else cv_final()]
        return await super().start_session(system_prompt, initial_user_msg)


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
    require_stage_cleared: bool = False,
    orch: Orchestrator | None = None,
) -> Job:
    """Poll the DB until job reaches target_state or timeout expires.

    If require_stage_cleared=True, also wait for current_stage to be None
    (useful for revision flow where job starts AND ends in 'review').

    When target_state is 'review' and orch is given, a job that parks at the CV gate
    (cv_review) is auto-approved (see _approve_cv) so the cover-letter lane starts —
    the two-lane pipeline no longer advances a job past the CV gate on its own.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    approved = False
    while True:
        async with factory() as s:
            job = await repo.get_job(s, job_id)
        if job is not None and job.state == target_state:
            if not require_stage_cleared or job.current_stage is None:
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
        if asyncio.get_running_loop().time() >= deadline:
            state_str = job.state if job else "None"
            stage_str = job.current_stage if job else "None"
            raise TimeoutError(
                f"Job {job_id} did not reach {target_state} (stage=None) within {timeout}s "
                f"(current state: {state_str}, stage: {stage_str})"
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
    require_stage_cleared: bool = False,
) -> None:
    """Run the orchestrator as a task, wait for all jobs to reach target_state, then stop.

    If require_stage_cleared=True, also waits until current_stage is None (for revision
    tests where the job starts and ends in the same state but must pass through running).
    """
    task = asyncio.create_task(orch.run())
    try:
        await asyncio.gather(
            *[
                asyncio.wait_for(
                    _poll_job_state(
                        factory,
                        jid,
                        target_state,
                        require_stage_cleared=require_stage_cleared,
                        orch=orch,
                    ),
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
# Scenario 1: Happy path — 2 jobs go all the way to approved
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Two jobs go pending → running → cv_done → running → review → approved."""

    async def test_two_jobs_reach_review(self, session_factory):
        """Both jobs must reach 'review' after running the full two-stage pipeline."""
        job1 = await _insert_job(session_factory, job_id="aabbccdd00000001")
        job2 = await _insert_job(session_factory, job_id="aabbccdd00000002")

        # Each backend_factory call returns a fresh backend, stage-discriminated on the
        # system prompt so a 2-job run doesn't interleave a cv_final into a cl slot.
        # The fit reply is scripted through the separate fit_backend_factory.
        # max_parallel=1 prevents concurrent SQLite writes through the shared StaticPool
        # connection — concurrency is tested separately in test_orchestrator.py.
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: _StageAwareDocBackend(),
            fit_backend_factory=lambda name: FakeAgentBackend([fit_reply()]),
            max_parallel=1,
        )

        await _run_orchestrator_until(
            orch,
            session_factory,
            [job1.id, job2.id],
            JobState.review,
        )

        async with session_factory() as s:
            j1 = await repo.get_job(s, job1.id)
            j2 = await repo.get_job(s, job2.id)
        assert j1.state == JobState.review
        assert j2.state == JobState.review

    async def test_both_documents_written(self, session_factory):
        """Both cv_adjust and cover_letter documents must be written per job."""
        job1 = await _insert_job(session_factory, job_id="bbccddee00000001")
        job2 = await _insert_job(session_factory, job_id="bbccddee00000002")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: _StageAwareDocBackend(),
            fit_backend_factory=lambda name: FakeAgentBackend([fit_reply()]),
            max_parallel=1,
        )
        await _run_orchestrator_until(
            orch, session_factory, [job1.id, job2.id], JobState.review
        )

        for job_id in [job1.id, job2.id]:
            async with session_factory() as s:
                cv_docs = await repo.get_documents(s, job_id, stage=Stage.cv_adjust)
                cl_docs = await repo.get_documents(s, job_id, stage=Stage.cover_letter)
            assert len(cv_docs) == 1, f"Job {job_id}: expected 1 cv doc"
            assert len(cl_docs) == 1, f"Job {job_id}: expected 1 cl doc"

    async def test_both_jobs_approved_with_pdfs(self, session_factory, tmp_path, monkeypatch):
        """Approve both jobs via the HTTP route; PDFs must be written to output dir.

        Per CLAUDE.md -> "Renderer invocation", rendering happens on review-entry (inside
        the orchestrator's cover_letter checkpoint), not on approve — so the fake renderer
        must be patched where stages.py actually calls it, and the orchestrator needs an
        output_dir or _render_for_review is skipped entirely.
        """
        fake_renderer = FakeRenderer()
        monkeypatch.setattr("jsa.pipeline.stages.renderer_for", lambda name: fake_renderer)

        job1 = await _insert_job(session_factory, job_id="ccddee0000000001", company="Alpha")
        job2 = await _insert_job(session_factory, job_id="ccddee0000000002", company="Beta")

        # Run pipeline until both jobs are in review
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: _StageAwareDocBackend(),
            fit_backend_factory=lambda name: FakeAgentBackend([fit_reply()]),
            max_parallel=1,
            output_dir=tmp_path / "output",
        )
        await _run_orchestrator_until(
            orch, session_factory, [job1.id, job2.id], JobState.review
        )

        # Build the app pointing at our shared session_factory and tmp_path output
        settings = Settings(
            output_dir=tmp_path / "output",
            db_path=tmp_path / "jsa.sqlite",  # not used — we patch session_factory
            backend="claude-cli",
            port=8765,
            no_browser=True,
        )
        # Patch Orchestrator.run to no-op so the app's startup doesn't start a new loop
        monkeypatch.setattr(Orchestrator, "run", lambda self: asyncio.sleep(9999))
        monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

        app = create_app(settings)

        async with app.router.lifespan_context(app):
            # Replace the app's session_factory with our test one
            app.state.session_factory = session_factory
            # Override output_dir in app settings
            app.state.settings.output_dir = tmp_path / "output"

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                for job in [job1, job2]:
                    resp = await client.post(f"/api/jobs/{job.id}/approve")
                    assert resp.status_code == 200, f"Approve failed for {job.id}: {resp.text}"
                    paths = resp.json()
                    assert "cv_pdf_path" in paths
                    assert "cl_pdf_path" in paths
                    # Verify PDF files exist at the returned paths
                    for path_key in ("cv_pdf_path", "cl_pdf_path"):
                        p = Path(paths[path_key])
                        assert p.exists(), f"{path_key} file does not exist: {p}"
                        assert p.read_bytes() == b"PDF", f"{path_key} file content unexpected"

        # Both jobs must be approved
        for job_id in [job1.id, job2.id]:
            async with session_factory() as s:
                j = await repo.get_job(s, job_id)
            assert j.state == JobState.approved, f"Job {job_id} not approved"

        # Pre-render fires twice per job now (see CLAUDE.md -> "Renderer invocation" / "Two-lane
        # pipeline / CV gate"): CV-only PDF+DOCX at the cv_review gate (2 renders), then both
        # artifacts' PDF+DOCX at review entry (4 renders) — 6 renders/job × 2 jobs = 12.
        # approve itself still renders nothing.
        assert len(fake_renderer.calls) == 12


# ---------------------------------------------------------------------------
# Scenario 2: Park & resume
# ---------------------------------------------------------------------------


class TestParkAndResume:
    """Job parks at awaiting_input, user answers, job resumes and completes."""

    async def test_job_parks_at_awaiting_input(self, session_factory):
        """First run: NEED_INPUT reply causes job to land in awaiting_input."""
        job = await _insert_job(session_factory, job_id="ddee001100000001")

        # fit_assessment runs first (FIT); cv_adjust then parks on NEED_INPUT.
        # Shared instance so replies are consumed across both stages in order.
        backend = FakeAgentBackend([fit_reply(), _needs_input_reply("What sector?")])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
        )
        await _run_orchestrator_until(
            orch, session_factory, [job.id], JobState.awaiting_input
        )

        async with session_factory() as s:
            j = await repo.get_job(s, job.id)
        assert j.state == JobState.awaiting_input

    async def test_follow_up_row_created_with_question(self, session_factory):
        """A FollowUp row containing the question must exist after parking."""
        job = await _insert_job(session_factory, job_id="ddee001100000002")

        # fit_assessment runs first (FIT); cv_adjust then parks on NEED_INPUT.
        backend = FakeAgentBackend([fit_reply(), _needs_input_reply("What sector?")])
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
        assert fus[0].question == "What sector?"

    async def test_job_resumes_to_review_after_answer(self, session_factory):
        """After user answers the follow-up, job continues to review."""
        job = await _insert_job(session_factory, job_id="ddee001100000003")

        # Step 1: park on NEED_INPUT during cv_adjust (fit_assessment runs first)
        orch_park = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend(
                [fit_reply(), _needs_input_reply("What sector?")]
            ),
        )
        await _run_orchestrator_until(
            orch_park, session_factory, [job.id], JobState.awaiting_input
        )

        # Step 2: user answers the follow-up
        async with session_factory() as s:
            fus = await repo.get_follow_ups(s, job.id, answered=False)
            assert len(fus) == 1
            fu = fus[0]
            fu_id = fu.id
        async with session_factory() as s:
            fu = await s.get(FollowUp, fu_id)
            fu.answer = "Technology"
            fu.answered_at = datetime.utcnow()
            await s.commit()

        # Step 3: resume — restore_session pops nothing, so cv_adjust consumes the first
        # scripted reply and cover_letter the second.
        backend_resume = FakeAgentBackend([cv_final(), cl_final()])
        orch_resume = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_resume,
        )
        orch_resume.kick()
        await _run_orchestrator_until(
            orch_resume, session_factory, [job.id], JobState.review
        )

        async with session_factory() as s:
            j = await repo.get_job(s, job.id)
        assert j.state == JobState.review

    async def test_message_rows_exist_after_full_run(self, session_factory):
        """After park/resume/complete, message rows for both stages must exist."""
        job = await _insert_job(session_factory, job_id="ddee001100000004")

        # Park (fit_assessment runs first)
        orch_park = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend(
                [fit_reply(), _needs_input_reply("Which sector?")]
            ),
        )
        await _run_orchestrator_until(
            orch_park, session_factory, [job.id], JobState.awaiting_input
        )

        # Answer
        async with session_factory() as s:
            fus = await repo.get_follow_ups(s, job.id, answered=False)
            fu_id = fus[0].id
        async with session_factory() as s:
            fu = await s.get(FollowUp, fu_id)
            fu.answer = "Finance"
            fu.answered_at = datetime.utcnow()
            await s.commit()

        # Resume — restore_session pops nothing, so cv_adjust consumes the first scripted
        # reply and cover_letter the second.
        backend_resume = FakeAgentBackend([cv_final(), cl_final()])
        orch_resume = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_resume,
        )
        orch_resume.kick()
        await _run_orchestrator_until(
            orch_resume, session_factory, [job.id], JobState.review
        )

        async with session_factory() as s:
            result = await s.execute(
                select(Message).where(Message.job_id == job.id)
            )
            msgs = list(result.scalars().all())

        # At minimum: system + user + assistant (cv_adjust park),
        # user-answer + assistant-final (resume), system + user + assistant (cover_letter)
        # = 8 messages. Just assert at least 3 per stage.
        assert len(msgs) >= 3, f"Expected at least 3 messages, got {len(msgs)}"
        stages_seen = {m.stage for m in msgs}
        assert Stage.cv_adjust in stages_seen
        assert Stage.cover_letter in stages_seen


# ---------------------------------------------------------------------------
# Scenario 3: Crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    """Simulate mid-run crash; verify recovery_sweep corrects state."""

    async def test_running_with_cv_doc_becomes_cv_done(self, session_factory):
        """3a: running(cover_letter) + cv_adjust doc → cv_done after sweep."""
        job_id = "eeff002200000001"
        async with session_factory() as s:
            job = Job(
                id=job_id,
                company="Acme",
                role="Engineer",
                link="https://acme.com/1",
                tier="A",
                jd="JD text",
                jd_hash="hash1234deadbeef",
                cv_text="CV text",
                state=JobState.running,
                current_stage=Stage.cover_letter,  # crashed mid cover_letter
            )
            s.add(job)
            # cv_adjust doc exists — CV stage had completed before crash
            cv_doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# CV done before crash",
            )
            s.add(cv_doc)
            await s.commit()

        async with session_factory() as s:
            await repo.recovery_sweep(s)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.cv_done
        assert j.current_stage is None

    async def test_running_no_docs_becomes_pending(self, session_factory):
        """3b: running + no docs → pending after sweep."""
        job_id = "eeff002200000002"
        async with session_factory() as s:
            job = Job(
                id=job_id,
                company="Acme",
                role="SWE",
                link="https://acme.com/2",
                tier="B",
                jd="JD text 2",
                jd_hash="hash5678deadbeef",
                cv_text="CV text 2",
                state=JobState.running,
                current_stage=Stage.cv_adjust,
            )
            s.add(job)
            await s.commit()

        async with session_factory() as s:
            await repo.recovery_sweep(s)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.pending
        assert j.current_stage is None

    async def test_recovery_leaves_awaiting_input_untouched(self, session_factory):
        """Jobs in awaiting_input must not be modified by recovery_sweep."""
        job_id = "eeff002200000003"
        async with session_factory() as s:
            job = Job(
                id=job_id,
                company="Acme",
                role="PM",
                link="https://acme.com/3",
                tier="C",
                jd="JD text 3",
                jd_hash="hash9999deadbeef",
                cv_text="CV text 3",
                state=JobState.awaiting_input,
                current_stage=Stage.cv_adjust,
            )
            s.add(job)
            fu = FollowUp(
                job_id=job_id,
                stage=Stage.cv_adjust,
                question="What is your target role?",
            )
            s.add(fu)
            await s.commit()

        async with session_factory() as s:
            await repo.recovery_sweep(s)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.awaiting_input
        assert j.current_stage == Stage.cv_adjust

    async def test_running_revising_cv_from_cv_review_rewinds_to_cv_review(self, session_factory):
        """A crash mid running(revising_cv), where the RevisionRequest.origin_state is
        'cv_review', must rewind to cv_review — not fall through to the cv_done
        Document-presence heuristic, which would silently "approve" a CV the user
        never approved (see CLAUDE.md → recovery_sweep docstring)."""
        job_id = "eeff002200000005"
        async with session_factory() as s:
            job = Job(
                id=job_id,
                company="Acme",
                role="Engineer",
                link="https://acme.com/5",
                tier="A",
                jd="JD text",
                jd_hash="hash5555deadbeef",
                cv_text="CV text",
                state=JobState.running,
                current_stage=Stage.revising_cv,
            )
            s.add(job)
            # cv_adjust doc exists from the original run — must NOT trigger cv_done
            cv_doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# CV before crash",
            )
            s.add(cv_doc)
            rev_req = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Make it shorter",
                origin_state="cv_review",
                consumed_at=None,
            )
            s.add(rev_req)
            await s.commit()

        async with session_factory() as s:
            await repo.recovery_sweep(s)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.cv_review
        assert j.current_stage == Stage.revising_cv

    async def test_recovered_job_continues_after_sweep(self, session_factory):
        """A job recovered to cv_done should be picked up by orchestrator and finish."""
        job_id = "eeff002200000004"
        async with session_factory() as s:
            # Simulate crashed state: running during cover_letter, cv_adjust completed
            job = Job(
                id=job_id,
                company="Beta Corp",
                role="Analyst",
                link="https://beta.com/4",
                tier="A",
                jd="JD for analyst role",
                jd_hash="hashanalystrole1",
                cv_text="Analyst CV text here",
                state=JobState.running,
                current_stage=Stage.cover_letter,
            )
            s.add(job)
            cv_doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# Analyst CV (pre-crash)",
            )
            s.add(cv_doc)
            await s.commit()

        # Run recovery sweep → job should go to cv_done
        async with session_factory() as s:
            await repo.recovery_sweep(s)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.cv_done

        # Now run the orchestrator — it should pick up cv_done and run cover_letter
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([cl_final()]),
        )
        await _run_orchestrator_until(orch, session_factory, [job_id], JobState.review)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.review


# ---------------------------------------------------------------------------
# Scenario 4: Revision flow
# ---------------------------------------------------------------------------


class TestRevisionFlow:
    """Job in review gets a revision request; new document version is written; state stays review."""

    async def _setup_review_job(self, session_factory, job_id: str) -> Job:
        """Create a job in review state with cv_adjust + cover_letter docs and message history."""
        async with session_factory() as s:
            job = Job(
                id=job_id,
                company="Gamma Inc",
                role="Dev",
                link="https://gamma.com/dev",
                tier="A",
                jd="Developer JD",
                jd_hash="hashdevjddead1234",
                cv_text="Developer CV text",
                state=JobState.review,
                current_stage=None,
            )
            s.add(job)

            # cv_adjust document version 1
            cv_doc = Document(
                job_id=job_id,
                stage=Stage.cv_adjust,
                version=1,
                markdown="# Original CV",
            )
            # cover_letter document version 1
            cl_doc = Document(
                job_id=job_id,
                stage=Stage.cover_letter,
                version=1,
                markdown="# Original Cover Letter",
            )
            s.add(cv_doc)
            s.add(cl_doc)

            # Message history for cv_adjust stage (required by restore_session)
            s.add(Message(job_id=job_id, stage=Stage.cv_adjust, role="user", content="Initial CV"))
            s.add(Message(job_id=job_id, stage=Stage.cv_adjust, role="assistant", content="# Original CV"))
            # Set per-stage session IDs so the BF-9 null-session guard does not fire.
            job.cv_session_id = "fake-session-cv-123"
            job.cl_session_id = "fake-session-cl-123"
            await s.commit()

        async with session_factory() as s:
            return await repo.get_job(s, job_id)

    async def test_revision_produces_new_document_version(self, session_factory):
        """After revision completes, a v2 cv_adjust document must exist."""
        job_id = "ffaa003300000001"
        job = await self._setup_review_job(session_factory, job_id)

        # Insert a RevisionRequest and set current_stage = revising_cv
        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
            rev_req = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Make it shorter",
            )
            s.add(rev_req)
            set_current_stage(j, Stage.revising_cv)
            j.updated_at = datetime.utcnow()
            s.add(j)
            await s.commit()

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([cv_final("# Shorter CV")]),
        )
        await _run_orchestrator_until(
            orch, session_factory, [job_id], JobState.review, require_stage_cleared=True
        )

        async with session_factory() as s:
            docs = await repo.get_documents(s, job_id, stage=Stage.cv_adjust)
        assert len(docs) == 2
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2]

    async def test_revision_request_consumed(self, session_factory):
        """RevisionRequest.consumed_at must be set after revision completes."""
        from sqlalchemy import select as sa_select

        job_id = "ffaa003300000002"
        await self._setup_review_job(session_factory, job_id)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
            rev_req = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Expand skills",
            )
            s.add(rev_req)
            set_current_stage(j, Stage.revising_cv)
            j.updated_at = datetime.utcnow()
            s.add(j)
            await s.commit()

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([cv_final("# Expanded CV")]),
        )
        await _run_orchestrator_until(
            orch, session_factory, [job_id], JobState.review, require_stage_cleared=True
        )

        async with session_factory() as s:
            result = await s.execute(
                sa_select(RevisionRequest).where(RevisionRequest.job_id == job_id)
            )
            rr = result.scalar_one()
        assert rr.consumed_at is not None

    async def test_state_stays_review_after_revision(self, session_factory):
        """Job state must remain 'review' after revision completes."""
        job_id = "ffaa003300000003"
        await self._setup_review_job(session_factory, job_id)

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
            rev_req = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Add certifications",
            )
            s.add(rev_req)
            set_current_stage(j, Stage.revising_cv)
            j.updated_at = datetime.utcnow()
            s.add(j)
            await s.commit()

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([cv_final("# CV + Certs")]),
        )
        await _run_orchestrator_until(
            orch, session_factory, [job_id], JobState.review, require_stage_cleared=True
        )

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
        assert j.state == JobState.review
        assert j.current_stage is None

    async def test_revised_document_content_matches_reply(self, session_factory):
        """The v2 document content must match the FakeAgentBackend's reply."""
        job_id = "ffaa003300000004"
        await self._setup_review_job(session_factory, job_id)

        revised_content = "# Revised CV with specific content"

        async with session_factory() as s:
            j = await repo.get_job(s, job_id)
            rev_req = RevisionRequest(
                job_id=job_id,
                target=Stage.cv_adjust,
                instruction="Make it more specific",
            )
            s.add(rev_req)
            set_current_stage(j, Stage.revising_cv)
            j.updated_at = datetime.utcnow()
            s.add(j)
            await s.commit()

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([cv_final(revised_content)]),
        )
        await _run_orchestrator_until(
            orch, session_factory, [job_id], JobState.review, require_stage_cleared=True
        )

        async with session_factory() as s:
            docs = await repo.get_documents(s, job_id, stage=Stage.cv_adjust)
        latest = max(docs, key=lambda d: d.version)
        assert latest.version == 2
        assert revised_content in latest.markdown
