"""Per-message backend/model attribution (`messages.backend_name` / `messages.model_name`).

The gap these columns close: `Job.backend_name` and `Job.model_name` are mutable
current-state fields. A BF-19 backend switch or a model-ladder hop rewrites them, so a
job that switched mid-flight reports its *latest* backend for its *whole* history --
including rows a different backend actually produced. These tests pin that each Message
row records the backend instance that served it, at the time it was written.

Uses FakeAgentBackend (scripted replies) + in-memory SQLite, mirroring test_stages.py.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.agents.google_cli import GoogleCliBackend
from jsa.db import repo
from jsa.db.models import Base, Job, JobState, Message, Stage
from jsa.pipeline.stages import PausedForInput, run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend
from tests.backend.fakes.finals import cl_final as _cl_final, cv_final as _cv_final


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
    job.state = JobState.pending
    await session.commit()
    return job


def _fit_final(text: str = "FIT\ngood match") -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{text}\n<<<END>>>", content=text, kind="final")


def _needs_input(question: str = "Which team?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content="",
        question=question,
        kind="needs_input",
    )


async def _rows(session: AsyncSession, job_id: str, stage: Stage) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(Message.job_id == job_id, Message.stage == stage)
        .order_by(Message.id.asc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# AgentBackend.model_id
# ---------------------------------------------------------------------------


class TestModelIdAccessor:
    def test_reads_the_instance_not_the_class(self):
        """One backend `name` can front many models, so a class-level answer would be
        wrong for all but one of them -- the same trap `supports_structured_output`
        documents. Two instances of the SAME class must report their own models."""
        a = FakeAgentBackend([], model="model-a")
        b = FakeAgentBackend([], model="model-b")
        assert (a.model_id, b.model_id) == ("model-a", "model-b")

    def test_none_for_a_backend_with_no_model_concept(self):
        """`google-cli` wraps the `agy` CLI, which has no model flag at all -- hence
        SUPPORTS_MODEL_SELECTION["google-cli"] is False. None is the correct answer
        there, not a bug, and it must not raise."""
        assert GoogleCliBackend().model_id is None

    def test_default_is_none_so_the_getattr_fallback_is_real(self):
        """A backend that never sets `_model` must still answer, via the getattr
        default -- that is what lets `model_id` live on the ABC with no per-backend
        boilerplate. FakeAgentBackend's `model` kwarg defaults to None, which is the
        same code path every pre-existing test already exercises implicitly."""
        assert FakeAgentBackend([]).model_id is None


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------


class TestStampedOnEveryRow:
    async def test_cv_adjust_rows_carry_the_serving_backend(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_cv_final()], name="mistral", model="mistral-small-2603")
        await run_stage(job, backend, Stage.cv_adjust, session)

        rows = await _rows(session, job.id, Stage.cv_adjust)
        assert rows, "expected system/user/assistant rows"
        # Every row in the checkpoint, not only the assistant one: a system/user row
        # records which backend the prompt was SENT to, which is half the trace.
        assert {(m.backend_name, m.model_name) for m in rows} == {
            ("mistral", "mistral-small-2603")
        }
        assert {m.role for m in rows} >= {"system", "user", "assistant"}

    async def test_parked_awaiting_input_rows_are_stamped(self, session):
        """The NEED_INPUT park writes its rows through a different checkpoint call
        (`_handle_needs_input`), which is the one site that had no `backend` in scope."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input()], name="openrouter", model="qwen-3")
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        rows = await _rows(session, job.id, Stage.cv_adjust)
        assert rows
        assert {(m.backend_name, m.model_name) for m in rows} == {("openrouter", "qwen-3")}

    async def test_model_name_is_null_when_the_backend_has_no_model(self, session):
        """NULL model_name is a legitimate recorded value (google-cli), distinct from
        'we forgot to record it' only by the backend_name sitting beside it."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_cv_final()], name="google-cli", model=None)
        await run_stage(job, backend, Stage.cv_adjust, session)

        rows = await _rows(session, job.id, Stage.cv_adjust)
        assert rows
        assert all(m.backend_name == "google-cli" and m.model_name is None for m in rows)


class TestFitModelIsAttributedSeparately:
    """The discriminating case. With `--fit-model` pinned the fit gate runs a DIFFERENT
    model on the same backend, and it never touches `Job.model_name` -- so sourcing
    attribution from the Job row would mislabel every fit row."""

    async def test_fit_rows_and_cv_rows_carry_different_models(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        general = FakeAgentBackend(
            [_cv_final()], name="opencode-go", model="big-expensive-model"
        )
        fit = FakeAgentBackend([_fit_final()], name="opencode-go", model="tiny-cheap-model")

        await run_stage(job, general, Stage.fit_assessment, session, fit_backend=fit)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.fit_done

        transition(refreshed, JobState.running, Stage.cv_adjust)
        await session.commit()
        await run_stage(refreshed, general, Stage.cv_adjust, session)

        fit_rows = await _rows(session, job.id, Stage.fit_assessment)
        cv_rows = await _rows(session, job.id, Stage.cv_adjust)
        assert fit_rows and cv_rows
        assert all(m.model_name == "tiny-cheap-model" for m in fit_rows)
        assert all(m.model_name == "big-expensive-model" for m in cv_rows)
        # Same backend name on both -- the model is the only discriminator here, which
        # is exactly why `Job.backend_name` alone could never have answered this.
        assert {m.backend_name for m in fit_rows + cv_rows} == {"opencode-go"}


class TestSurvivesABackendSwitch:
    """The whole point of the columns. `backend_switch_reset` wipes only the FAILED
    stage's Message rows -- a `cover_letter`-stage switch explicitly keeps the
    `cv_adjust` rows (see its `failed_stage` mapping). Those surviving rows are exactly
    the ones `Job.backend_name` starts lying about the moment it is reassigned."""

    async def test_one_job_carries_two_backends_across_its_stages(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        first = FakeAgentBackend([_cv_final()], name="opencode-zen", model="nemotron-3")
        await run_stage(job, first, Stage.cv_adjust, session)

        job = await repo.get_job(session, job.id)
        job.backend_name = "opencode-zen"
        # Approve the CV and enter the cover-letter lane, where the failure lands.
        await repo.checkpoint(session, job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        # BF-19: cover_letter fails, the job advances to the next backend and rewinds to
        # cv_done. Only the cover_letter rows are deleted; cv_adjust's survive.
        await repo.backend_switch_reset(session, job, "anthropic", Stage.cover_letter)

        second = FakeAgentBackend([_cl_final()], name="anthropic", model="claude-haiku-4-5")
        job = await repo.get_job(session, job.id)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()
        await run_stage(job, second, Stage.cover_letter, session)

        refreshed = await repo.get_job(session, job.id)
        # The Job row only knows where it ENDED UP -- this is the field that used to be
        # the only answer available, and it is wrong for half this job's history.
        assert refreshed.backend_name == "anthropic"

        cv_rows = await _rows(session, job.id, Stage.cv_adjust)
        cl_rows = await _rows(session, job.id, Stage.cover_letter)
        assert cv_rows and cl_rows
        assert all(
            (m.backend_name, m.model_name) == ("opencode-zen", "nemotron-3") for m in cv_rows
        )
        assert all(
            (m.backend_name, m.model_name) == ("anthropic", "claude-haiku-4-5")
            for m in cl_rows
        )


class TestReplayIsBlindToTheNewColumns:
    """`_load_history` projects rows to a two-field HistoryTurn(role, content), so the
    attribution columns are structurally invisible to replay -- same property
    `Message.reasoning` documents. If this breaks, resumed sessions start feeding
    attribution metadata back into the model."""

    async def test_history_turns_expose_only_role_and_content(self, session):
        from jsa.pipeline.stages import _load_history

        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()
        backend = FakeAgentBackend([_cv_final()], name="mistral", model="mistral-small-2603")
        await run_stage(job, backend, Stage.cv_adjust, session)

        turns = await _load_history(session, job.id, Stage.cv_adjust)
        assert turns
        for turn in turns:
            assert set(vars(turn)) == {"role", "content"}


class TestLegacyRowsStayNull:
    """Rows written before these columns existed cannot be backfilled -- the
    attribution was never captured. NULL must be a legal read, not a crash."""

    async def test_a_message_written_without_attribution_reads_back_null(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        await repo.checkpoint(
            session,
            job,
            JobState.cv_review,
            None,
            messages=[{"role": "assistant", "content": "legacy turn"}],
        )

        rows = await _rows(session, job.id, Stage.cv_adjust)
        assert [(m.backend_name, m.model_name) for m in rows] == [(None, None)]
