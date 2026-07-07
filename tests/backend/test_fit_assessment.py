"""Tests for the fit-assessment stage and its surrounding plumbing.

Covers:
- run_stage(fit_assessment): FIT → fit_done; UNFIT / unclear / needs_input → unfit
- the FIT/UNFIT verdict parser (fail-to-modal default)
- state-machine transitions for the new states
- list_runnable_jobs picks up fit_done
- the dispatch mapping (pending → fit_assessment, fit_done → cv_adjust)

Uses FakeAgentBackend (scripted replies) + in-memory SQLite, mirroring test_stages.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, Document, Job, JobState, Message, Stage
from jsa.pipeline.orchestrator import _next_stage_for
from jsa.pipeline.stages import _parse_fit_verdict, run_stage
from jsa.pipeline.state_machine import InvalidTransition, transition
from tests.backend.fakes.fake_backend import FakeAgentBackend


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


async def _insert_job(session: AsyncSession, **overrides) -> Job:
    data = dict(
        id="aabbccdd00112233",
        company="Acme",
        role="Senior Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
        cv_text="Curriculum vitae text",
    )
    data.update(overrides)
    job = await repo.upsert_job(session, data)
    job.state = JobState.pending  # simulate an already-launched job
    await session.commit()
    return job


def _final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


def _needs_input(question: str = "Which team is this for?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


async def _run_fit(session: AsyncSession, job: Job, reply: AgentReply) -> Job:
    """Transition to running(fit_assessment) and run the stage with one scripted reply."""
    transition(job, JobState.running, Stage.fit_assessment)
    await session.commit()
    backend = FakeAgentBackend([reply])
    await run_stage(job, backend, Stage.fit_assessment, session)
    return await repo.get_job(session, job.id)


# ---------------------------------------------------------------------------
# Verdict parser (pure)
# ---------------------------------------------------------------------------


class TestParseFitVerdict:
    def test_fit_single_word(self):
        is_fit, reason = _parse_fit_verdict(_final("FIT"))
        assert is_fit is True
        assert reason is None

    def test_fit_with_trailing_text(self):
        is_fit, _ = _parse_fit_verdict(_final("FIT\nStrong overlap with the JD."))
        assert is_fit is True

    def test_unfit_with_reason_on_following_line(self):
        is_fit, reason = _parse_fit_verdict(
            _final("UNFIT\nRole needs 15+ years; CV shows 4.")
        )
        assert is_fit is False
        assert reason == "Role needs 15+ years; CV shows 4."

    def test_unfit_single_line_with_colon(self):
        is_fit, reason = _parse_fit_verdict(_final("UNFIT: unrelated field"))
        assert is_fit is False
        assert reason == "unrelated field"

    def test_needs_input_fails_to_modal_with_question(self):
        is_fit, reason = _parse_fit_verdict(_needs_input("What seniority?"))
        assert is_fit is False
        assert reason == "What seniority?"

    def test_unparseable_verdict_fails_to_modal(self):
        is_fit, reason = _parse_fit_verdict(_final("I think maybe this could work?"))
        assert is_fit is False
        assert reason  # surfaces something to the user, never silently passes

    @pytest.mark.parametrize("payload", ["**FIT**", "Verdict: FIT — strong overlap", "## FIT", "FIT ✅"])
    def test_decorated_fit_still_classifies_as_fit(self, payload):
        """A model that decorates the verdict must not be parked at the modal."""
        is_fit, reason = _parse_fit_verdict(_final(payload))
        assert is_fit is True
        assert reason is None

    @pytest.mark.parametrize("payload", ["**UNFIT** — unrelated field", "Verdict: UNFIT\nBig gap."])
    def test_decorated_unfit_still_classifies_as_unfit(self, payload):
        is_fit, reason = _parse_fit_verdict(_final(payload))
        assert is_fit is False
        assert reason and "UNFIT" not in reason.upper()  # verdict token stripped from reason


# ---------------------------------------------------------------------------
# run_stage(fit_assessment)
# ---------------------------------------------------------------------------


class TestFitAssessmentStage:
    async def test_fit_transitions_to_fit_done(self, session):
        job = await _insert_job(session)
        refreshed = await _run_fit(session, job, _final("FIT"))
        assert refreshed.state == JobState.fit_done
        assert refreshed.current_stage is None
        assert refreshed.fit_reason is None

    async def test_unfit_transitions_to_unfit_and_stores_reason(self, session):
        job = await _insert_job(session)
        refreshed = await _run_fit(
            session, job, _final("UNFIT\nCompletely unrelated field.")
        )
        assert refreshed.state == JobState.unfit
        assert refreshed.current_stage is None
        assert refreshed.fit_reason == "Completely unrelated field."

    async def test_needs_input_parks_as_unfit(self, session):
        job = await _insert_job(session)
        refreshed = await _run_fit(session, job, _needs_input("Which role?"))
        assert refreshed.state == JobState.unfit
        assert refreshed.fit_reason == "Which role?"

    async def test_garbage_verdict_parks_as_unfit(self, session):
        job = await _insert_job(session)
        refreshed = await _run_fit(session, job, _final("hmm not sure"))
        assert refreshed.state == JobState.unfit
        assert refreshed.fit_reason

    async def test_protocol_error_parks_as_unfit(self, session):
        """A sentinel-less reply (ProtocolError in start_session) → unfit, not failed."""
        from jsa.agents.protocol import ProtocolError

        class NoSentinelBackend(FakeAgentBackend):
            async def start_session(self, system_prompt, initial_user_msg):
                raise ProtocolError("no sentinel block")

        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()
        await run_stage(job, NoSentinelBackend([]), Stage.fit_assessment, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.unfit
        assert refreshed.fit_reason

    async def test_no_document_is_written(self, session):
        """The assessment stores its reason in a column, not a Document."""
        job = await _insert_job(session)
        await _run_fit(session, job, _final("UNFIT\nGap."))
        docs = await repo.get_documents(session, job.id)
        assert docs == []

    async def test_messages_persisted(self, session):
        job = await _insert_job(session)
        await _run_fit(session, job, _final("FIT"))
        result = await session.execute(
            select(Message).where(
                Message.job_id == job.id, Message.stage == Stage.fit_assessment
            )
        )
        roles = [m.role for m in result.scalars().all()]
        assert "system" in roles and "user" in roles and "assistant" in roles


# ---------------------------------------------------------------------------
# State machine + dispatch + runnable
# ---------------------------------------------------------------------------


class TestStateMachine:
    def _job(self, state, stage=None):
        j = Job(id="x", company="c", role="r", link="l", tier="A", jd="", jd_hash="h",
                cv_text="", state=state, current_stage=stage)
        return j

    def test_running_to_fit_done(self):
        j = self._job(JobState.running, Stage.fit_assessment)
        transition(j, JobState.fit_done, None)
        assert j.state == JobState.fit_done

    def test_running_to_unfit(self):
        j = self._job(JobState.running, Stage.fit_assessment)
        transition(j, JobState.unfit, None)
        assert j.state == JobState.unfit

    def test_unfit_to_fit_done_ignore(self):
        j = self._job(JobState.unfit)
        transition(j, JobState.fit_done, None)
        assert j.state == JobState.fit_done

    def test_unfit_to_dismissed(self):
        j = self._job(JobState.unfit)
        transition(j, JobState.dismissed, None)
        assert j.state == JobState.dismissed

    def test_fit_done_to_running_cv_adjust(self):
        j = self._job(JobState.fit_done)
        transition(j, JobState.running, Stage.cv_adjust)
        assert j.state == JobState.running and j.current_stage == Stage.cv_adjust

    def test_unfit_to_approved_is_rejected(self):
        j = self._job(JobState.unfit)
        with pytest.raises(InvalidTransition):
            transition(j, JobState.approved, None)


class TestDispatchMapping:
    def _job(self, state, stage=None):
        return Job(id="x", company="c", role="r", link="l", tier="A", jd="", jd_hash="h",
                   cv_text="", state=state, current_stage=stage)

    def test_pending_dispatches_fit_assessment(self):
        assert _next_stage_for(self._job(JobState.pending)) == Stage.fit_assessment

    def test_fit_done_dispatches_cv_adjust(self):
        assert _next_stage_for(self._job(JobState.fit_done)) == Stage.cv_adjust


class TestRunnable:
    async def test_fit_done_job_is_runnable(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()
        await repo.checkpoint(session, job, JobState.fit_done, None)

        runnable = await repo.list_runnable_jobs(session)
        assert job.id in {j.id for j in runnable}

    async def test_unfit_job_is_not_runnable(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()
        await repo.checkpoint(session, job, JobState.unfit, None)

        runnable = await repo.list_runnable_jobs(session)
        assert job.id not in {j.id for j in runnable}
