"""Regression tests for BF-10: revision stages infinite NEED_INPUT loop.

Before the fix, the revising_cv / revising_cl branch in run_stage always
re-fetched the revision instruction and re-sent it. When the agent asked a
follow-up mid-revision, the resume path would send the instruction again instead
of the user's answer, producing an infinite NEED_INPUT loop.

The fix adds _is_revision_resume(), which checks for a FollowUp with
stage==revision_stage and answered_at > RevisionRequest.created_at. Fresh
revision: no such row → instruction is sent. Resume: such a row exists → the
user's answer is sent.

Three tests:

Test 1: test_revising_cl_resume_sends_answer_not_instruction
  Discriminating test — fails against pre-fix code.
  Verifies the second send_message receives the user's answer ("finalize"),
  not the original instruction.

Test 2: test_revising_cv_resume_sends_answer_not_instruction
  Mirror of Test 1 for the revising_cv path.

Test 3: test_second_revision_after_first_completes_sends_instruction
  Multi-revision edge case: old FollowUp rows from a completed prior revision
  must NOT trigger the resume path when a fresh second revision starts.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle
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
from jsa.pipeline.stages import PausedForInput, run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


# ---------------------------------------------------------------------------
# TrackingFakeBackend: records text argument of each send_message call.
# ---------------------------------------------------------------------------


class TrackingFakeBackend(FakeAgentBackend):
    """FakeAgentBackend that records the text arg of every send_message call.

    Used to assert that the resume path sends the user's answer, not the
    revision instruction.
    """

    def __init__(self, replies: list[AgentReply]) -> None:
        super().__init__(replies)
        self.send_message_calls: list[str] = []

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        # Assign a stable external_id so per-stage session IDs are non-None.
        external_id = str(uuid4())
        handle = FakeSessionHandle(id=str(uuid4()), external_id=external_id)
        reply = self._pop_reply()
        return handle, reply

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        self.send_message_calls.append(text)
        return await super().send_message(handle, text)


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
        # Add BF-9 columns if they were not yet included in Base (migration guard)
        for col in ("cv_session_id", "cl_session_id"):
            try:
                from sqlalchemy import text
                await conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} VARCHAR(128)"))
            except Exception:
                pass  # column already exists in the model
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    """Yield a single AsyncSession for most tests."""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _final_reply(content: str) -> AgentReply:
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _needs_input_reply(question: str) -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


async def _insert_job(session: AsyncSession, job_id: str = "bf10test0011aabb") -> Job:
    data = dict(
        id=job_id,
        company="BF10Co",
        role="Engineer",
        link="https://bf10co.com/job",
        tier="A",
        jd="Job description for BF-10 regression test.",
        jd_hash="bf10hash00deadbf",
        cv_text="Curriculum vitae for BF-10 test.",
    )
    job = await repo.upsert_job(session, data)
    # upsert_job creates fresh jobs as `queued`; simulate an already-launched job.
    job.state = JobState.pending
    await session.commit()
    return job


async def _run_cv_adjust(session: AsyncSession, job: Job, backend: TrackingFakeBackend) -> Job:
    """Transition job to running(cv_adjust), run the stage, return refreshed job."""
    transition(job, JobState.running, Stage.cv_adjust)
    await session.commit()
    await run_stage(job, backend, Stage.cv_adjust, session)
    return await repo.get_job(session, job.id)


async def _run_cover_letter(session: AsyncSession, job: Job, backend: TrackingFakeBackend) -> Job:
    """Transition job to running(cover_letter), run the stage, return refreshed job."""
    transition(job, JobState.running, Stage.cover_letter)
    await session.commit()
    await run_stage(job, backend, Stage.cover_letter, session)
    return await repo.get_job(session, job.id)


async def _insert_revision_request(
    session: AsyncSession, job_id: str, target: Stage, instruction: str
) -> RevisionRequest:
    rr = RevisionRequest(
        job_id=job_id,
        target=target,
        instruction=instruction,
        consumed_at=None,
    )
    session.add(rr)
    await session.commit()
    return rr


async def _answer_open_follow_up(session: AsyncSession, job_id: str, stage: Stage, answer: str) -> FollowUp:
    """Find the unanswered FollowUp for (job, stage) and record the user's answer."""
    result = await session.execute(
        select(FollowUp).where(
            FollowUp.job_id == job_id,
            FollowUp.stage == stage,
            FollowUp.answered_at.is_(None),
        )
    )
    fu = result.scalar_one()
    return await repo.answer_follow_up(session, fu.id, answer)


# ---------------------------------------------------------------------------
# Test 1: revising_cl resume sends the user's answer, not the instruction
# ---------------------------------------------------------------------------


class TestRevisingClResumesSendAnswer:
    """BF-10 discriminating test for the revising_cl path."""

    async def test_revising_cl_resume_sends_answer_not_instruction(self, session):
        """The second run_stage call for revising_cl must send the user's answer.

        Setup:
        1. Run cv_adjust → FINAL.
        2. Run cover_letter → FINAL.
        3. Insert RevisionRequest(target=cover_letter, instruction="Remove closing phrase").
        4. Transition to running(revising_cl). Run revising_cl → NEED_INPUT
           (parks to awaiting_input).
        5. Answer the follow-up with "finalize".
        6. Transition to running(revising_cl). Run revising_cl → FINAL (resume).

        Assertions:
        - send_message was called exactly TWICE total.
        - calls[0] == instruction text (the revision instruction).
        - calls[1] == "finalize" (the user's answer).
        - The resulting Document (stage=cover_letter, version=2) contains REVISED_CL content.
        - RevisionRequest is consumed.
        """
        instruction = "Remove the closing phrase"
        user_answer = "finalize"

        job = await _insert_job(session)

        backend = TrackingFakeBackend([
            _final_reply("# CV v1"),                                          # cv_adjust
            _final_reply("# Cover Letter v1"),                                # cover_letter
            _needs_input_reply("Here is the draft. Type 'finalize' to approve."),  # revising_cl (1st call)
            _final_reply("# REVISED_CL_CONTENT"),                            # revising_cl (2nd call / resume)
        ])

        # Stage 1: cv_adjust
        job = await _run_cv_adjust(session, job, backend)
        assert job.cv_session_id is not None, "cv_session_id must be set after cv_adjust"

        # Stage 2: cover_letter
        job = await _run_cover_letter(session, job, backend)
        assert job.cl_session_id is not None, "cl_session_id must be set after cover_letter"
        assert job.state == JobState.review

        # Insert RevisionRequest
        await _insert_revision_request(session, job.id, Stage.cover_letter, instruction)

        # Stage 3a: revising_cl — fresh run → NEED_INPUT
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.revising_cl, session)

        job = await repo.get_job(session, job.id)
        assert job.state == JobState.awaiting_input
        assert job.current_stage == Stage.revising_cl

        # Verify the fresh run sent the instruction
        assert len(backend.send_message_calls) == 1, (
            f"Expected 1 send_message call after fresh revision, "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )
        assert backend.send_message_calls[0] == instruction, (
            f"First send_message should be the instruction, "
            f"got: {backend.send_message_calls[0]!r}"
        )

        # Answer the follow-up
        await _answer_open_follow_up(session, job.id, Stage.revising_cl, user_answer)

        # Stage 3b: revising_cl — resume run → FINAL
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        await run_stage(job, backend, Stage.revising_cl, session)

        # --- Primary assertions ---

        # send_message called exactly twice total
        assert len(backend.send_message_calls) == 2, (
            f"Expected 2 send_message calls total (instruction + answer), "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )

        # Second call received the user's answer, NOT the instruction again
        assert backend.send_message_calls[1] == user_answer, (
            f"Resume send_message should receive the user answer {user_answer!r}, "
            f"got {backend.send_message_calls[1]!r}. "
            f"(Pre-fix bug: instruction would be re-sent instead.)"
        )

        # Resulting document is version 2 under cover_letter stage
        docs = await repo.get_documents(session, job.id, Stage.cover_letter)
        assert len(docs) == 2, f"Expected 2 CL documents (v1 and v2), got {len(docs)}"
        latest = max(docs, key=lambda d: d.version)
        assert latest.version == 2
        assert "REVISED_CL_CONTENT" in latest.markdown, (
            f"Latest document should contain revised content, got: {latest.markdown!r}"
        )

        # RevisionRequest is consumed
        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        rr = result.scalar_one()
        assert rr.consumed_at is not None, "RevisionRequest must be consumed after revision completion"

        # Job is back in review
        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review
        assert job.current_stage is None


# ---------------------------------------------------------------------------
# Test 2: revising_cv resume sends the user's answer, not the instruction
# ---------------------------------------------------------------------------


class TestRevisingCvResumesSendAnswer:
    """BF-10 discriminating test for the revising_cv path."""

    async def test_revising_cv_resume_sends_answer_not_instruction(self, session):
        """The second run_stage call for revising_cv must send the user's answer.

        Mirror of Test 1, but targeting the revising_cv path.
        """
        instruction = "Shorten the summary section"
        user_answer = "looks good"

        job = await _insert_job(session, job_id="bf10cvtest0011aa")

        backend = TrackingFakeBackend([
            _final_reply("# CV v1"),                                          # cv_adjust
            _final_reply("# Cover Letter v1"),                                # cover_letter
            _needs_input_reply("Draft ready. Does this look good?"),          # revising_cv (1st call)
            _final_reply("# REVISED_CV_CONTENT"),                            # revising_cv (2nd call / resume)
        ])

        # Stage 1: cv_adjust
        job = await _run_cv_adjust(session, job, backend)
        assert job.cv_session_id is not None

        # Stage 2: cover_letter
        job = await _run_cover_letter(session, job, backend)
        assert job.cl_session_id is not None
        assert job.state == JobState.review

        # Insert RevisionRequest for cv_adjust
        await _insert_revision_request(session, job.id, Stage.cv_adjust, instruction)

        # Stage 3a: revising_cv — fresh run → NEED_INPUT
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.revising_cv, session)

        job = await repo.get_job(session, job.id)
        assert job.state == JobState.awaiting_input
        assert job.current_stage == Stage.revising_cv

        # First send_message sent the instruction
        assert len(backend.send_message_calls) == 1
        assert backend.send_message_calls[0] == instruction, (
            f"First send_message should be the instruction, got: {backend.send_message_calls[0]!r}"
        )

        # Answer the follow-up
        await _answer_open_follow_up(session, job.id, Stage.revising_cv, user_answer)

        # Stage 3b: revising_cv — resume → FINAL
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        await run_stage(job, backend, Stage.revising_cv, session)

        # --- Primary assertions ---

        assert len(backend.send_message_calls) == 2, (
            f"Expected 2 send_message calls, got {len(backend.send_message_calls)}: "
            f"{backend.send_message_calls}"
        )

        assert backend.send_message_calls[1] == user_answer, (
            f"Resume send_message should receive {user_answer!r}, "
            f"got {backend.send_message_calls[1]!r}. "
            f"(Pre-fix bug: instruction would be re-sent.)"
        )

        # New document is version 2 under cv_adjust stage
        docs = await repo.get_documents(session, job.id, Stage.cv_adjust)
        assert len(docs) == 2, f"Expected 2 CV documents (v1 and v2), got {len(docs)}"
        latest = max(docs, key=lambda d: d.version)
        assert latest.version == 2
        assert "REVISED_CV_CONTENT" in latest.markdown

        # RevisionRequest consumed
        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        rr = result.scalar_one()
        assert rr.consumed_at is not None

        # Job back in review
        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review
        assert job.current_stage is None


# ---------------------------------------------------------------------------
# Test 3: A second revision after the first completes sends the new instruction
# ---------------------------------------------------------------------------


class TestSecondRevisionAfterFirstCompletes:
    """Verifies the multi-revision edge case.

    The discriminator must not treat Message rows from a prior completed revision
    as evidence of a mid-revision park. After the first revision completes (no
    FollowUp created), the second revision must be treated as fresh and must
    send the new instruction.
    """

    async def test_second_revision_sends_new_instruction(self, session):
        """After a completed first revision, the second revision sends its own instruction.

        This catches the case where _load_history(stage) is used as the
        discriminator: it would find messages from the first revision and
        erroneously take the resume path, sending the wrong text.

        With the fix (_is_revision_resume), no FollowUp with answered_at >
        second_rev_req.created_at exists when the second revision starts fresh,
        so the correct (fresh) path is taken.
        """
        first_instruction = "Make the summary shorter"
        second_instruction = "Add measurable impact metrics to bullet points"

        job = await _insert_job(session, job_id="bf10sec0011aabb")

        backend = TrackingFakeBackend([
            _final_reply("# CV v1"),                        # cv_adjust
            _final_reply("# Cover Letter v1"),              # cover_letter
            _final_reply("# REVISED_CV_V2 (short)"),       # first revising_cl — completes immediately
            _final_reply("# REVISED_CV_V3 (metrics)"),     # second revising_cl — must get second_instruction
        ])

        # Stage 1: cv_adjust
        job = await _run_cv_adjust(session, job, backend)

        # Stage 2: cover_letter
        job = await _run_cover_letter(session, job, backend)
        assert job.state == JobState.review

        # First revision: insert RevisionRequest #1, run revising_cl → FINAL immediately (no park)
        await _insert_revision_request(session, job.id, Stage.cover_letter, first_instruction)
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        await run_stage(job, backend, Stage.revising_cl, session)

        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review, "Job should be back in review after first revision"

        # Verify first revision consumed the first instruction
        assert len(backend.send_message_calls) == 1
        assert backend.send_message_calls[0] == first_instruction

        # Verify RevisionRequest #1 is consumed
        result = await session.execute(
            select(RevisionRequest)
            .where(RevisionRequest.job_id == job.id)
            .order_by(RevisionRequest.id.asc())
        )
        all_rrs = list(result.scalars().all())
        assert all_rrs[0].consumed_at is not None, "First RevisionRequest must be consumed"

        # Second revision: insert RevisionRequest #2 (AFTER #1 is consumed — unique index constraint)
        await _insert_revision_request(session, job.id, Stage.cover_letter, second_instruction)
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        await run_stage(job, backend, Stage.revising_cl, session)

        # --- Primary assertions ---

        # Two send_message calls total (one per revision)
        assert len(backend.send_message_calls) == 2, (
            f"Expected 2 send_message calls (one per revision), "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )

        # Second call must be the NEW instruction, not the answer from a FollowUp
        # (which wouldn't exist here) or the first instruction
        assert backend.send_message_calls[1] == second_instruction, (
            f"Second revision send_message should be the second instruction "
            f"{second_instruction!r}, got {backend.send_message_calls[1]!r}. "
            f"(Bug: old message history would erroneously trigger resume path.)"
        )

        # Three cover_letter documents: v1, v2, v3
        docs = await repo.get_documents(session, job.id, Stage.cover_letter)
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2, 3], f"Expected versions [1, 2, 3], got {versions}"
        latest = max(docs, key=lambda d: d.version)
        assert "REVISED_CV_V3" in latest.markdown

        # Both RevisionRequests consumed
        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        all_rrs = list(result.scalars().all())
        assert len(all_rrs) == 2
        for rr in all_rrs:
            assert rr.consumed_at is not None, (
                f"RevisionRequest {rr.id} (instruction={rr.instruction!r}) must be consumed"
            )

        # Job still in review
        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review
        assert job.current_stage is None


# ---------------------------------------------------------------------------
# Test 4: Second revision (fresh) after first revision parked mid-way and completed
# ---------------------------------------------------------------------------


class TestSecondRevisionAfterFirstParked:
    """Verifies the discriminator's key invariant: old answered FollowUp rows
    from a completed prior revision (which included a mid-revision park) do NOT
    trigger the resume path when a brand-new second revision starts.

    Scenario:
    - First revision parks (FollowUp answered_at = T2).
    - First revision resumes and completes (RevisionRequest #1 consumed).
    - Second revision inserted at T3 > T2 (RevisionRequest #2).
    - _is_revision_resume for the second revision:
        FollowUp.answered_at (T2) < RevisionRequest #2.created_at (T3) → False → fresh path.
    - So send_message receives the new instruction, NOT "finalize".
    """

    async def test_second_revision_fresh_after_first_parked(self, session):
        """Second fresh revision starts correctly after first revision parked mid-way.

        Sequence:
        1. cv_adjust → FINAL.
        2. cover_letter → FINAL.
        3. RevisionRequest #1 ("Remove closing phrase"). Transition to running(revising_cl).
           Run revising_cl → NEED_INPUT (parks to awaiting_input).
        4. Answer the follow-up with "finalize".
        5. Transition to running(revising_cl). Run revising_cl → FINAL (resume). #1 consumed.
        6. RevisionRequest #2 ("Change the tone to formal"). Transition to running(revising_cl).
           Run revising_cl → FINAL. Must send instruction from #2, not "finalize".

        Assertions:
        - send_message_calls has 3 entries total:
            [0] == first_instruction   (fresh first revision)
            [1] == "finalize"          (resume of first revision — user's answer)
            [2] == second_instruction  (fresh second revision — must NOT be "finalize")
        - cover_letter documents: versions [1, 2, 3].
        - Both RevisionRequests consumed.
        - Job ends in review, current_stage is None.
        """
        first_instruction = "Remove closing phrase"
        user_answer = "finalize"
        second_instruction = "Change the tone to formal"

        job = await _insert_job(session, job_id="bf10parked001122")

        # 5 scripted replies consumed in this order:
        #   start_session: cv_adjust   → FINAL
        #   start_session: cover_letter → FINAL
        #   send_message: revising_cl #1 fresh → NEED_INPUT
        #   send_message: revising_cl #1 resume → FINAL
        #   send_message: revising_cl #2 fresh → FINAL
        backend = TrackingFakeBackend([
            _final_reply("# CV v1"),                                              # cv_adjust
            _final_reply("# Cover Letter v1"),                                    # cover_letter
            _needs_input_reply("Here is the draft, finalize?"),                   # revising_cl #1 fresh
            _final_reply("# REVISED_CL_V2 (closing removed)"),                   # revising_cl #1 resume
            _final_reply("# REVISED_CL_V3 (formal tone)"),                       # revising_cl #2 fresh
        ])

        # Stage 1: cv_adjust
        job = await _run_cv_adjust(session, job, backend)
        assert job.cv_session_id is not None, "cv_session_id must be set after cv_adjust"

        # Stage 2: cover_letter
        job = await _run_cover_letter(session, job, backend)
        assert job.cl_session_id is not None, "cl_session_id must be set after cover_letter"
        assert job.state == JobState.review

        # --- First revision: parks mid-way ---

        await _insert_revision_request(session, job.id, Stage.cover_letter, first_instruction)
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        # First call: fresh → NEED_INPUT
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.revising_cl, session)

        job = await repo.get_job(session, job.id)
        assert job.state == JobState.awaiting_input
        assert job.current_stage == Stage.revising_cl

        # Verify the fresh run sent the instruction
        assert len(backend.send_message_calls) == 1, (
            f"Expected 1 send_message call after fresh first revision, "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )
        assert backend.send_message_calls[0] == first_instruction, (
            f"First send_message should be the first instruction, "
            f"got: {backend.send_message_calls[0]!r}"
        )

        # Answer the follow-up (simulates user responding in the Inbox)
        await _answer_open_follow_up(session, job.id, Stage.revising_cl, user_answer)

        # --- First revision: resumes and completes ---

        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        await run_stage(job, backend, Stage.revising_cl, session)

        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review, (
            f"Job should be back in review after first revision completes, got: {job.state}"
        )
        assert job.current_stage is None

        # Verify resume sent the user's answer
        assert len(backend.send_message_calls) == 2, (
            f"Expected 2 send_message calls after resume, "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )
        assert backend.send_message_calls[1] == user_answer, (
            f"Resume send_message should be the user's answer {user_answer!r}, "
            f"got: {backend.send_message_calls[1]!r}"
        )

        # Verify first RevisionRequest is consumed
        result = await session.execute(
            select(RevisionRequest)
            .where(RevisionRequest.job_id == job.id)
            .order_by(RevisionRequest.id.asc())
        )
        all_rrs = list(result.scalars().all())
        assert len(all_rrs) == 1
        assert all_rrs[0].consumed_at is not None, "First RevisionRequest must be consumed"

        # Verify v1 and v2 documents exist for cover_letter
        docs_after_first = await repo.get_documents(session, job.id, Stage.cover_letter)
        versions_after_first = sorted(d.version for d in docs_after_first)
        assert versions_after_first == [1, 2], (
            f"Expected versions [1, 2] after first revision, got {versions_after_first}"
        )

        # --- Second revision: fresh (key discriminator boundary) ---

        await _insert_revision_request(session, job.id, Stage.cover_letter, second_instruction)
        transition(job, JobState.running, Stage.revising_cl)
        await session.commit()

        # This is the discriminating run: _is_revision_resume must return False
        # because the first revision's FollowUp.answered_at < RevisionRequest #2.created_at.
        await run_stage(job, backend, Stage.revising_cl, session)

        # --- Primary assertions ---

        # Three send_message calls total across all revisions
        assert len(backend.send_message_calls) == 3, (
            f"Expected 3 send_message calls total (first_instruction, finalize, second_instruction), "
            f"got {len(backend.send_message_calls)}: {backend.send_message_calls}"
        )

        # The third call must be the second instruction, NOT "finalize".
        # If the discriminator erroneously detects a resume (using old FollowUp), it
        # would re-send "finalize" — this assertion catches that exact bug.
        assert backend.send_message_calls[2] == second_instruction, (
            f"Second fresh revision send_message must be {second_instruction!r}, "
            f"got {backend.send_message_calls[2]!r}. "
            f"(Bug: naive discriminator finds old FollowUp and sends 'finalize' instead.)"
        )

        # The second call was the resume answer, not re-sent instruction
        assert backend.send_message_calls[1] == user_answer, (
            f"Resume send_message should have been {user_answer!r}, "
            f"got {backend.send_message_calls[1]!r}"
        )

        # Three cover_letter documents: v1 (cover_letter stage), v2 (1st revision), v3 (2nd revision)
        docs = await repo.get_documents(session, job.id, Stage.cover_letter)
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2, 3], (
            f"Expected cover_letter document versions [1, 2, 3], got {versions}"
        )
        latest = max(docs, key=lambda d: d.version)
        assert "REVISED_CL_V3" in latest.markdown, (
            f"Latest document should contain second revision content, got: {latest.markdown!r}"
        )

        # Both RevisionRequests must be consumed
        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        all_rrs = list(result.scalars().all())
        assert len(all_rrs) == 2
        for rr in all_rrs:
            assert rr.consumed_at is not None, (
                f"RevisionRequest {rr.id} (instruction={rr.instruction!r}) must be consumed"
            )

        # Job ends in review with no active stage
        job = await repo.get_job(session, job.id)
        assert job.state == JobState.review
        assert job.current_stage is None
