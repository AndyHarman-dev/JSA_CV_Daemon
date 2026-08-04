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

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentLimitReached, AgentReply
from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Base, Document, Job, JobState, Message, Stage
from jsa.pipeline.orchestrator import Orchestrator, _next_stage_for
from jsa.pipeline.stages import PausedForInput, _parse_fit_verdict, run_stage
from jsa.server import make_backend_factory
from jsa.pipeline.state_machine import InvalidTransition, transition
from tests.backend.fakes.fake_backend import CapturingBackend, FakeAgentBackend


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


_CapturingBackend = CapturingBackend


class TestFitAssessmentConsumesBaseStructure:
    async def test_uses_markdown_rendered_structure_not_job_cv_text(self, session, tmp_path):
        structure_path = tmp_path / "cv_structure.json"
        structure_path.write_text(json.dumps({
            "contact": {"name": "Jane Doe", "email": "jane@x.com"},
            "sections": [
                {"name": "Summary", "text": "Backend engineer."},
                {"name": "Distinctive Section Name", "items": ["A distinctive skill"]},
            ],
        }), encoding="utf-8")

        job = await _insert_job(session, cv_text="STALE RAW CV TEXT — should never appear")
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        backend = _CapturingBackend([_final("FIT")])
        await run_stage(
            job, backend, Stage.fit_assessment, session, 
            cv_structure_path=structure_path,
        )

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "Distinctive Section Name" in msg  # structure content, rendered to markdown
        assert "A distinctive skill" in msg
        assert "STALE RAW CV TEXT" not in msg  # job.cv_text is deprecated, never injected
        assert "CV TEXT:" not in msg

    async def test_no_cv_block_when_structure_absent(self, session, tmp_path):
        missing = tmp_path / "nope.json"
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        backend = _CapturingBackend([_final("FIT")])
        await run_stage(job, backend, Stage.fit_assessment, session, cv_structure_path=missing)

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "CV:" not in msg
        assert "CV TEXT:" not in msg


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


# ---------------------------------------------------------------------------
# Separate fit-assessment model (Settings.fit_model / --fit-model)
# ---------------------------------------------------------------------------


class TestFitBackendInjection:
    """run_stage's `fit_backend` param: the fit gate may run on a different backend
    instance (different model) than the rest of the pipeline. Injected by the caller —
    stages.py must never construct it, or jsa.pipeline <-> jsa.server goes circular."""

    async def test_injected_fit_backend_runs_the_stage(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        main = CapturingBackend([_final("FIT\nmain backend should not be used")])
        fit = CapturingBackend([_final("FIT\ngood match")])

        await run_stage(job, main, Stage.fit_assessment, session, fit_backend=fit)

        assert fit.captured_initial_msg is not None, "fit_backend should have run the stage"
        assert main.captured_initial_msg is None, "main backend must not be touched"
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.fit_done

    async def test_falls_back_to_main_backend_when_not_injected(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        main = CapturingBackend([_final("FIT\ngood match")])
        await run_stage(job, main, Stage.fit_assessment, session)

        assert main.captured_initial_msg is not None
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.fit_done

    async def test_unfit_verdict_from_injected_backend_is_honoured(self, session):
        """The substituted model's verdict drives the gate — including the UNFIT path
        and its reason, which is what surfaces in the frontend modal."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        fit = FakeAgentBackend([_final("UNFIT\nno Kubernetes experience")])
        await run_stage(
            job, FakeAgentBackend([]), Stage.fit_assessment, session, fit_backend=fit
        )

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.unfit
        assert "Kubernetes" in (refreshed.fit_reason or "")

    async def test_fit_backend_is_ignored_by_non_fit_stages(self, session):
        """`fit_backend` is read only in the fit_assessment branch; cv_adjust must still
        use the main backend even when one is passed."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()
        await repo.checkpoint(session, job, JobState.fit_done, None)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        main = CapturingBackend([_needs_input("Which team?")])
        never = CapturingBackend([_final("should never run")])

        with pytest.raises(PausedForInput):
            await run_stage(job, main, Stage.cv_adjust, session, fit_backend=never)

        assert main.captured_initial_msg is not None
        assert never.captured_initial_msg is None


class TestBackendFactoryOverrides:
    """make_backend_factory's model_override / timeout_override — the mechanism behind
    Settings.fit_model. Critically, overriding must NOT flatten the per-backend timeout
    mapping (anthropic uses anthropic_timeout, CLI backends use agent_timeout)."""

    @staticmethod
    def _settings(**overrides) -> Settings:
        base = dict(
            model="claude-opus-4-5",
            anthropic_timeout=180.0,
            agent_timeout=600.0,
            fit_model=None,
            fit_timeout=None,
        )
        base.update(overrides)
        return Settings(**base)

    def _fit_factory(self, settings: Settings):
        """Mirrors server.py's orchestrator wiring."""
        return make_backend_factory(
            settings,
            model_override=settings.fit_model,
            timeout_override=settings.fit_timeout,
        )

    def test_no_override_is_indistinguishable_from_the_main_factory(self):
        """Feature is inert until configured: fit_model=None must not change anything."""
        settings = self._settings()
        for name in ("anthropic", "claude-cli"):
            main = make_backend_factory(settings)(name)
            fit = self._fit_factory(settings)(name)
            assert fit._model == main._model == "claude-opus-4-5"
            assert fit._timeout == main._timeout

    def test_fit_model_overrides_only_the_model(self):
        settings = self._settings(fit_model="claude-haiku-4-5")
        fit = self._fit_factory(settings)("anthropic")
        assert fit._model == "claude-haiku-4-5"
        assert make_backend_factory(settings)("anthropic")._model == "claude-opus-4-5"

    def test_anthropic_keeps_anthropic_timeout_when_fit_timeout_unset(self):
        """Regression: a single shared fit timeout silently gave anthropic 600s
        instead of its 180s anthropic_timeout."""
        settings = self._settings(fit_model="claude-haiku-4-5")
        assert self._fit_factory(settings)("anthropic")._timeout == 180.0
        assert self._fit_factory(settings)("claude-cli")._timeout == 600.0

    def test_fit_timeout_applies_to_every_backend_when_set(self):
        settings = self._settings(fit_timeout=45.0)
        for name in ("anthropic", "claude-cli", "google-cli"):
            assert self._fit_factory(settings)(name)._timeout == 45.0

    def test_google_cli_never_receives_a_model_kwarg(self):
        """GoogleCliBackend.__init__ takes no `model` — passing one is a TypeError."""
        settings = self._settings(fit_model="gemini-2.5-flash-lite")
        backend = self._fit_factory(settings)("google-cli")
        assert backend.name == "google-cli"
        assert not hasattr(backend, "_model")


async def _run_orch_until(orch, factory, job_id: str, target: JobState, timeout: float = 10.0) -> None:
    """Run the orchestrator until `job_id` reaches `target`, then stop it."""
    task = asyncio.create_task(orch.run())
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            async with factory() as s:
                job = await repo.get_job(s, job_id)
            if job is not None and job.state == target:
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise TimeoutError(f"Job {job_id} did not reach {target} within {timeout}s")
            await asyncio.sleep(0.05)
    finally:
        orch._stopping = True
        orch.kick()
        await asyncio.wait_for(task, timeout=5.0)


class TestOrchestratorFitBackendWiring:
    """The orchestrator half of the wiring: which name the fit factory is called with,
    and that a limit signal from the fit backend still reaches the BF-19 chain."""

    async def test_fit_factory_is_built_from_the_jobs_active_backend(self, session_factory):
        """BF-19: after a limit-triggered switch, job.backend_name has moved down the
        chain — the fit gate must follow it rather than pinning to backends[0]."""
        async with session_factory() as s:
            job = await _insert_job(s)
            job.backend_name = "google-cli"  # chain[1], i.e. post-switch
            await s.commit()
            job_id = job.id

        seen: list[str] = []

        def fit_factory(name: str):
            seen.append(name)
            return FakeAgentBackend([_final("UNFIT\nnot a match")])

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([]),
            backends=["claude-cli", "google-cli"],
            fit_backend_factory=fit_factory,
        )
        await _run_orch_until(orch, session_factory, job_id, JobState.unfit)

        assert seen == ["google-cli"], f"fit factory should follow job.backend_name, got {seen}"

    async def test_limit_from_fit_backend_walks_the_chain_instead_of_unfit(self, session_factory):
        """A quota signal on the fit gate is the chain's job, not a verdict.
        _run_fit_assessment catches only ProtocolError, so AgentLimitReached must
        propagate to _handle_limit_reached — never get swallowed into `unfit`."""
        async with session_factory() as s:
            job = await _insert_job(s)
            job_id = job.id

        class _LimitBackend(FakeAgentBackend):
            def __init__(self) -> None:
                super().__init__([])

            async def start_session(self, system_prompt, initial_user_msg):
                raise AgentLimitReached("429 Too Many Requests")

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: FakeAgentBackend([]),
            backends=["claude-cli", "google-cli"],
            fit_backend_factory=lambda name: _LimitBackend(),
        )
        # Both chain entries raise → chain exhausted → failed (not unfit).
        await _run_orch_until(orch, session_factory, job_id, JobState.failed)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job_id)
        assert refreshed.state == JobState.failed
        assert "Backend limit reached" in (refreshed.error or "")
