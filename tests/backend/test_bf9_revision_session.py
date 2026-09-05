"""Regression tests for BF-9: CV/CL revision wrong-session bug.

Before the fix, run_stage passed job.session_external_id to restore_session
during revisions. After both stages complete, session_external_id holds the
cover_letter session UUID — so revising_cv would resume the wrong conversation.

The fix adds job.cv_session_id and job.cl_session_id, and the revision path
now selects the per-stage ID rather than session_external_id.

These tests verify:
1. revising_cv calls restore_session with cv_session_id (not session_external_id)
2. revising_cl calls restore_session with cl_session_id
3. A second CV revision still uses cv_session_id (revisions don't overwrite it)
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cl_final, cv_final, tool_loop_miss


# ---------------------------------------------------------------------------
# Extended FakeAgentBackend that records restore_session calls and assigns
# distinct external_ids so cv and cl sessions are distinguishable.
# ---------------------------------------------------------------------------


class TrackingFakeBackend(FakeAgentBackend):
    """FakeAgentBackend that:
    - Assigns a unique external_id for each start_session call.
    - Records the external_id passed to every restore_session call.
    """

    def __init__(self, replies: list[AgentReply]) -> None:
        super().__init__(replies)
        # Each call to start_session produces a fresh UUID stored here.
        self.started_external_ids: list[str] = []
        # Each call to restore_session appends (external_id_received,).
        self.restore_calls: list[str | None] = []

    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[FakeSessionHandle, AgentReply]:
        external_id = str(uuid4())
        self.started_external_ids.append(external_id)
        handle = FakeSessionHandle(id=str(uuid4()), external_id=external_id)
        reply = self._pop_reply()
        return handle, reply

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> FakeSessionHandle:
        self.restore_calls.append(external_id)
        return FakeSessionHandle(id=str(uuid4()), external_id=external_id)


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
        # Add the new columns if they don't already exist (mirrors init_db)
        for col in ("cv_session_id", "cl_session_id"):
            try:
                from sqlalchemy import text
                await conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} VARCHAR(128)"))
            except Exception:
                pass  # column already exists
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


async def _insert_job(session: AsyncSession) -> Job:
    data = dict(
        id="bf9test00001111",
        company="TestCo",
        role="Engineer",
        link="https://testco.com/job",
        tier="A",
        jd="Job description for BF-9 regression test.",
        jd_hash="bf9hash0deadbeef",
        cv_text="Curriculum vitae for BF-9 test.",
    )
    job = await repo.upsert_job(session, data)
    # upsert_job creates fresh jobs as `queued`; simulate an already-launched job.
    job.state = JobState.pending
    await session.commit()
    return job


async def _add_cv_adjust_messages(session: AsyncSession, job_id: str) -> None:
    """Insert message history rows for cv_adjust stage (needed by restore_session path)."""
    session.add(Message(job_id=job_id, stage=Stage.cv_adjust, role="user", content="Initial CV"))
    session.add(Message(job_id=job_id, stage=Stage.cv_adjust, role="assistant", content="# CV v1"))
    await session.commit()


async def _add_cover_letter_messages(session: AsyncSession, job_id: str) -> None:
    """Insert message history rows for cover_letter stage."""
    session.add(Message(job_id=job_id, stage=Stage.cover_letter, role="user", content="Write CL"))
    session.add(Message(job_id=job_id, stage=Stage.cover_letter, role="assistant", content="# CL v1"))
    await session.commit()


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


# ---------------------------------------------------------------------------
# Test 1: revising_cv uses cv_session_id, not session_external_id
# ---------------------------------------------------------------------------


class TestRevisingCvUsesCvSessionId:
    async def test_revising_cv_uses_cv_session_id(self, session):
        """restore_session during revising_cv receives cv_session_id, not session_external_id.

        Scenario:
        1. Run cv_adjust → cv_session_id is set to the cv handle's external_id.
        2. Run cover_letter → cl_session_id is set; session_external_id is now the CL UUID.
        3. Insert RevisionRequest, transition to revising_cv, run_stage.
        4. Assert restore_session was called with cv_session_id (not session_external_id).
        """
        job = await _insert_job(session)

        # --- Stage 1: cv_adjust (fresh session) ---
        cv_content = "# CV v1 — adjusted"
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = TrackingFakeBackend([
            cv_final(cv_content),          # cv_adjust reply
            cl_final(),                    # cover_letter reply
            # cv_adjust's Document now has a real `.structured` payload, so revising_cv
            # genuinely attempts the tool loop first (prompt rung, since
            # TrackingFakeBackend never sets supports_native_tools) — see
            # tool_loop_miss's docstring. That attempt consumes one reply and gives up,
            # then rung 3 (unchanged) consumes the next.
            tool_loop_miss(),               # revising_cv: tool-loop attempt (discarded)
            cv_final("REVISED_CV_MARKER"),  # revising_cv reply (rung 3)
        ])

        await run_stage(job, backend, Stage.cv_adjust, session)

        # After cv_adjust, verify cv_session_id is set and equals the first external_id
        job_after_cv = await repo.get_job(session, job.id)
        cv_session_id = job_after_cv.cv_session_id
        assert cv_session_id is not None, "cv_session_id must be set after cv_adjust"
        assert cv_session_id == backend.started_external_ids[0]
        assert job_after_cv.session_external_id == cv_session_id

        # --- Stage 2: cover_letter (fresh session) ---
        transition(job_after_cv, JobState.running, Stage.cover_letter)
        await session.commit()

        await run_stage(job_after_cv, backend, Stage.cover_letter, session)

        job_after_cl = await repo.get_job(session, job.id)
        cl_session_id = job_after_cl.cl_session_id
        assert cl_session_id is not None, "cl_session_id must be set after cover_letter"
        assert cl_session_id == backend.started_external_ids[1]

        # Precondition: session_external_id is now the CL session UUID (the bug trigger)
        assert job_after_cl.session_external_id == cl_session_id, (
            "session_external_id should be cl's UUID at this point"
        )
        assert cl_session_id != cv_session_id, (
            "cv and cl session IDs must differ for the test to be meaningful"
        )

        # --- Stage 3: revising_cv ---
        # Insert cv_adjust messages so history load succeeds
        await _add_cv_adjust_messages(session, job.id)
        await _insert_revision_request(session, job.id, Stage.cv_adjust, "Make it shorter")

        transition(job_after_cl, JobState.running, Stage.revising_cv)
        await session.commit()

        await run_stage(job_after_cl, backend, Stage.revising_cv, session)

        # Assert restore_session was called with cv_session_id (not cl_session_id) —
        # TWO calls this time: the tool-loop attempt's restore, then rung 3's.
        assert len(backend.restore_calls) == 2, (
            f"Expected exactly 2 restore_session calls (tool-loop attempt + rung 3), "
            f"got {len(backend.restore_calls)}"
        )
        assert backend.restore_calls == [cv_session_id, cv_session_id], (
            f"Both restore_session calls should receive cv_session_id={cv_session_id!r}, "
            f"got {backend.restore_calls!r} (cl_session_id={cl_session_id!r})"
        )

        # Assert the new document contains the revised content (not CL content)
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        latest_doc = max(docs, key=lambda d: d.version)
        assert "REVISED_CV_MARKER" in latest_doc.markdown, (
            f"Revised CV document should contain 'REVISED_CV_MARKER', "
            f"got: {latest_doc.markdown!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: revising_cl uses cl_session_id
# ---------------------------------------------------------------------------


class TestRevisingClUsesClSessionId:
    async def test_revising_cl_uses_cl_session_id(self, session):
        """restore_session during revising_cl receives cl_session_id.

        Scenario:
        1. Run cv_adjust → cv_session_id set.
        2. Run cover_letter → cl_session_id set; session_external_id = cl_session_id.
        3. Insert RevisionRequest for cover_letter, transition to revising_cl, run_stage.
        4. Assert restore_session was called with cl_session_id.
        """
        job = await _insert_job(session)

        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = TrackingFakeBackend([
            cv_final("CV v1"),
            cl_final(),
            # See tool_loop_miss's docstring: cover_letter's Document now has a real
            # `.structured` payload, so revising_cl genuinely attempts the tool loop
            # (prompt rung) first.
            tool_loop_miss(),
            cl_final(
                "This is the REVISED_CL_MARKER revision of the cover letter, rewritten to "
                "better highlight the candidate's relevant production experience for this role."
            ),
        ])

        # Stage 1: cv_adjust
        await run_stage(job, backend, Stage.cv_adjust, session)

        job_after_cv = await repo.get_job(session, job.id)
        cv_session_id = job_after_cv.cv_session_id
        assert cv_session_id is not None

        # Stage 2: cover_letter
        transition(job_after_cv, JobState.running, Stage.cover_letter)
        await session.commit()

        await run_stage(job_after_cv, backend, Stage.cover_letter, session)

        job_after_cl = await repo.get_job(session, job.id)
        cl_session_id = job_after_cl.cl_session_id
        assert cl_session_id is not None
        assert cl_session_id != cv_session_id, (
            "cv and cl session IDs must differ for the test to be meaningful"
        )

        # Precondition check: session_external_id is the CL UUID
        assert job_after_cl.session_external_id == cl_session_id

        # Stage 3: revising_cl
        await _add_cover_letter_messages(session, job.id)
        await _insert_revision_request(session, job.id, Stage.cover_letter, "Rewrite intro")

        transition(job_after_cl, JobState.running, Stage.revising_cl)
        await session.commit()

        await run_stage(job_after_cl, backend, Stage.revising_cl, session)

        # Assert restore_session was called with cl_session_id — TWO calls this time:
        # the tool-loop attempt's restore, then rung 3's.
        assert len(backend.restore_calls) == 2, (
            f"Expected exactly 2 restore_session calls (tool-loop attempt + rung 3), "
            f"got {len(backend.restore_calls)}"
        )
        assert backend.restore_calls == [cl_session_id, cl_session_id], (
            f"Both restore_session calls should receive cl_session_id={cl_session_id!r}, "
            f"got {backend.restore_calls!r} (cv_session_id={cv_session_id!r})"
        )

        # Assert the new document is stored under cover_letter stage
        docs = await repo.get_documents(session, job.id, stage=Stage.cover_letter)
        latest_doc = max(docs, key=lambda d: d.version)
        assert "REVISED_CL_MARKER" in latest_doc.markdown


# ---------------------------------------------------------------------------
# Test 3: A second CV revision still uses the same cv_session_id
# ---------------------------------------------------------------------------


class TestSecondCvRevisionUsesSameCvSessionId:
    async def test_revising_cv_after_revising_cv_uses_same_cv_session_id(self, session):
        """cv_session_id is not overwritten by the first revision run.

        Scenario:
        1. Run cv_adjust → cv_session_id set.
        2. Run cover_letter → cl_session_id set.
        3. First revising_cv → completes, job back to review.
        4. Second revising_cv → restore_session still receives the original cv_session_id.
        """
        job = await _insert_job(session)

        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = TrackingFakeBackend([
            cv_final("CV v1"),                     # cv_adjust
            cl_final(),                            # cover_letter
            # Each revising_cv turn spends TWO replies: the tool loop's prompt-rung
            # attempt (discarded) then rung 3 — see tool_loop_miss's docstring.
            tool_loop_miss(),                      # first revising_cv: tool-loop attempt
            cv_final("REVISED_CV_V2_MARKER"),      # first revising_cv
            tool_loop_miss(),                      # second revising_cv: tool-loop attempt
            cv_final("REVISED_CV_V3_MARKER"),      # second revising_cv
        ])

        # Stage 1: cv_adjust
        await run_stage(job, backend, Stage.cv_adjust, session)

        job_after_cv = await repo.get_job(session, job.id)
        cv_session_id = job_after_cv.cv_session_id
        assert cv_session_id is not None

        # Stage 2: cover_letter
        transition(job_after_cv, JobState.running, Stage.cover_letter)
        await session.commit()

        await run_stage(job_after_cv, backend, Stage.cover_letter, session)

        job_after_cl = await repo.get_job(session, job.id)
        cl_session_id = job_after_cl.cl_session_id
        assert cl_session_id != cv_session_id

        # Stage 3: First revising_cv
        await _add_cv_adjust_messages(session, job.id)
        await _insert_revision_request(session, job.id, Stage.cv_adjust, "Shorten it")

        transition(job_after_cl, JobState.running, Stage.revising_cv)
        await session.commit()

        await run_stage(job_after_cl, backend, Stage.revising_cv, session)

        # Verify first revision used cv_session_id — on BOTH restores it makes (the
        # tool loop owns its own restore_session for the rung it enters, then rung 3
        # restores again; see tool_loop_miss's docstring).
        assert backend.restore_calls[:2] == [cv_session_id, cv_session_id], (
            "First revising_cv must use cv_session_id on every restore it makes, "
            f"got {backend.restore_calls[:2]!r}"
        )

        job_after_rev1 = await repo.get_job(session, job.id)
        assert job_after_rev1.state == JobState.review

        # cv_session_id must remain unchanged after the revision
        assert job_after_rev1.cv_session_id == cv_session_id, (
            "cv_session_id must not be overwritten by the revision run"
        )

        # Stage 4: Second revising_cv
        await _insert_revision_request(session, job.id, Stage.cv_adjust, "Expand skills")

        transition(job_after_rev1, JobState.running, Stage.revising_cv)
        await session.commit()

        await run_stage(job_after_rev1, backend, Stage.revising_cv, session)

        # The second revision's restore_session calls also use cv_session_id
        assert len(backend.restore_calls) == 4, (
            f"Expected 4 restore_session calls total (two revisions x tool-loop "
            f"attempt + rung 3), got {len(backend.restore_calls)}"
        )
        assert backend.restore_calls[2:] == [cv_session_id, cv_session_id], (
            f"Second revising_cv must still use cv_session_id={cv_session_id!r}, "
            f"got {backend.restore_calls[2:]!r}"
        )

        # Three documents for cv_adjust: v1, v2, v3
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2, 3], f"Expected versions [1, 2, 3], got {versions}"
        latest = max(docs, key=lambda d: d.version)
        assert "REVISED_CV_V3_MARKER" in latest.markdown
