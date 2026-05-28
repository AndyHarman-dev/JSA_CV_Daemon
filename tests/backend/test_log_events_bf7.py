"""Tests for BF-7: LogEvent/ErrorEvent publishing at pipeline milestones.

Covers six publish points introduced in BF-7:
  Point A (orchestrator): LogEvent info "Picked up: starting {stage.value}"
  Point B (orchestrator): LogEvent warn "Failed to start {stage.value}: {exc}"
  Point C (orchestrator): LogEvent error + ErrorEvent when _run_one raises
  Point D (stages):       LogEvent info "Starting stage: {stage.value}"
  Point E (stages):       LogEvent info "Stage {stage.value}: FINAL received"
  Point F (stages):       LogEvent info "Stage {stage.value}: NEED_INPUT"

All tests use asyncio_mode = "auto" (configured in pyproject.toml).
Fakes over mocks for backends; patch is used only for bus.publish.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, SessionHandle
from jsa.db import repo
from jsa.db.models import Base, Job, JobState, Stage
from jsa.pipeline.orchestrator import Orchestrator
from jsa.pipeline.stages import PausedForInput, run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend


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


@pytest.fixture
async def session(session_factory):
    """Yield a single AsyncSession for direct stages tests."""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _job_data(job_id: str | None = None) -> dict:
    if job_id is None:
        job_id = uuid4().hex[:16]
    return dict(
        id=job_id,
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
        cv_text="Curriculum vitae text",
    )


async def _insert_job(factory, **overrides) -> Job:
    """Insert a fresh pending job using the session factory."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        await s.commit()
    return job


async def _insert_job_in_session(session: AsyncSession, **overrides) -> Job:
    """Insert a fresh pending job into an existing session."""
    data = _job_data(**overrides)
    job = await repo.upsert_job(session, data)
    await session.commit()
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
# FailingBackend for Point C
# ---------------------------------------------------------------------------


class FailingBackend(AgentBackend):
    """Backend that raises RuntimeError on start_session to trigger Point C."""

    name = "failing"

    def __init__(self, error_msg: str = "agent exploded") -> None:
        self._error_msg = error_msg

    async def start_session(self, system_prompt: str, initial_user_msg: str):
        raise RuntimeError(self._error_msg)

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> SessionHandle:
        raise RuntimeError(self._error_msg)

    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply:
        raise RuntimeError(self._error_msg)

    async def end_session(self, handle: SessionHandle) -> None:
        pass


# ---------------------------------------------------------------------------
# Point A: LogEvent published when job is picked up
# ---------------------------------------------------------------------------


class TestPointA_PickedUpLogEvent:
    async def test_log_event_info_published_on_pickup(self, session_factory):
        """Orchestrator publishes a 'log' event with level=info containing 'Picked up'
        and the stage name after transitioning a job to running."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FakeAgentBackend([_final_reply()]),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.review
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        log_events = [e for e in calls if e.get("type") == "log"]

        pickup_events = [
            e
            for e in log_events
            if e.get("level") == "info" and "Picked up" in e.get("text", "")
        ]
        assert pickup_events, (
            f"Expected at least one 'log' event with level='info' and 'Picked up' in text. "
            f"Got log events: {log_events}"
        )

        first_pickup = pickup_events[0]
        assert "cv_adjust" in first_pickup["text"], (
            f"Expected 'cv_adjust' in pickup log text, got: {first_pickup['text']}"
        )

    async def test_log_event_includes_stage_name(self, session_factory):
        """The 'Picked up' LogEvent text includes the stage value (cv_adjust)."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FakeAgentBackend([_final_reply()]),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.review
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        log_events = [e for e in calls if e.get("type") == "log"]
        pickup_texts = [
            e["text"]
            for e in log_events
            if "Picked up" in e.get("text", "")
        ]
        # At least one pickup event should mention the stage
        assert any("cv_adjust" in t for t in pickup_texts), (
            f"Expected 'cv_adjust' in at least one pickup log text. Texts: {pickup_texts}"
        )

    async def test_log_event_job_id_matches(self, session_factory):
        """The 'Picked up' LogEvent has the correct job_id field."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FakeAgentBackend([_final_reply()]),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.review
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        pickup_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and "Picked up" in e.get("text", "")
            and e.get("job_id") == job.id
        ]
        assert pickup_events, (
            f"Expected a pickup LogEvent with job_id={job.id!r}"
        )


# ---------------------------------------------------------------------------
# Point B: LogEvent warn published when transition to running fails
# ---------------------------------------------------------------------------


class TestPointB_FailedToStartLogEvent:
    async def test_log_event_warn_published_on_transition_failure(self, session_factory):
        """When the running-transition raises, a 'log' event with level='warn'
        containing 'Failed to start' is published."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with patch(
                "jsa.pipeline.orchestrator.transition",
                side_effect=RuntimeError("transition boom"),
            ):
                orch = Orchestrator(
                    db_session_factory=session_factory,
                    backend_factory=lambda: FakeAgentBackend([_final_reply()]),
                )
                # The transition always fails, so the job never goes to running.
                # We just run one dispatch cycle and then stop.
                task = asyncio.create_task(orch.run())
                await asyncio.sleep(0.3)
                orch._stopping = True
                orch.kick()
                await asyncio.wait_for(task, timeout=5.0)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        warn_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and e.get("level") == "warn"
            and "Failed to start" in e.get("text", "")
        ]
        assert warn_events, (
            f"Expected a 'log' event with level='warn' and 'Failed to start' in text. "
            f"Got: {calls}"
        )

    async def test_warn_log_event_includes_stage_name(self, session_factory):
        """The 'Failed to start' LogEvent text includes the stage value."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with patch(
                "jsa.pipeline.orchestrator.transition",
                side_effect=RuntimeError("transition boom"),
            ):
                orch = Orchestrator(
                    db_session_factory=session_factory,
                    backend_factory=lambda: FakeAgentBackend([_final_reply()]),
                )
                task = asyncio.create_task(orch.run())
                await asyncio.sleep(0.3)
                orch._stopping = True
                orch.kick()
                await asyncio.wait_for(task, timeout=5.0)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        warn_texts = [
            e["text"]
            for e in calls
            if e.get("type") == "log" and "Failed to start" in e.get("text", "")
        ]
        assert any("cv_adjust" in t for t in warn_texts), (
            f"Expected 'cv_adjust' in warn log text. Got: {warn_texts}"
        )


# ---------------------------------------------------------------------------
# Point C: LogEvent error + ErrorEvent published when _run_one raises
# ---------------------------------------------------------------------------


class TestPointC_ErrorEventOnRunOneFailure:
    async def test_log_event_error_published_on_exception(self, session_factory):
        """When the agent raises, a 'log' event with level='error' is published
        after the job is marked failed."""
        error_msg = "agent exploded badly"
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FailingBackend(error_msg),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.failed
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        error_log_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and e.get("level") == "error"
            and error_msg in e.get("text", "")
        ]
        assert error_log_events, (
            f"Expected 'log' event with level='error' and text containing {error_msg!r}. "
            f"Got: {calls}"
        )

    async def test_error_event_published_on_exception(self, session_factory):
        """When the agent raises, an 'error' event with the exception message is published."""
        error_msg = "agent exploded badly"
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FailingBackend(error_msg),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.failed
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        error_events = [
            e
            for e in calls
            if e.get("type") == "error"
            and error_msg in e.get("message", "")
        ]
        assert error_events, (
            f"Expected 'error' event with message containing {error_msg!r}. "
            f"Got: {calls}"
        )

    async def test_both_log_and_error_events_published_together(self, session_factory):
        """Both the LogEvent(error) and ErrorEvent are published when _run_one fails."""
        error_msg = "twin events agent crash"
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FailingBackend(error_msg),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.failed
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        has_error_log = any(
            e.get("type") == "log"
            and e.get("level") == "error"
            and error_msg in e.get("text", "")
            for e in calls
        )
        has_error_event = any(
            e.get("type") == "error"
            and error_msg in e.get("message", "")
            for e in calls
        )
        assert has_error_log, "Missing 'log' event with level='error'"
        assert has_error_event, "Missing 'error' event"

    async def test_error_log_event_job_id_matches(self, session_factory):
        """The error LogEvent has the correct job_id."""
        error_msg = "job id check crash"
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FailingBackend(error_msg),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.failed
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]
        matched = [
            e
            for e in calls
            if e.get("type") == "log"
            and e.get("level") == "error"
            and e.get("job_id") == job.id
        ]
        assert matched, f"Expected error log event with job_id={job.id!r}"


# ---------------------------------------------------------------------------
# Point D: LogEvent published at stage entry
# ---------------------------------------------------------------------------


class TestPointD_StartingStageLogEvent:
    async def test_log_event_starting_stage_published_cv_adjust(self, session):
        """run_stage publishes 'Starting stage: cv_adjust' at entry."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        starting_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and e.get("level") == "info"
            and "Starting stage:" in e.get("text", "")
            and "cv_adjust" in e.get("text", "")
        ]
        assert starting_events, (
            f"Expected 'log' event with 'Starting stage:' and 'cv_adjust'. Got: {calls}"
        )

    async def test_log_event_starting_stage_published_cover_letter(self, session):
        """run_stage publishes 'Starting stage: cover_letter' for the cover_letter stage."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cover_letter, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        starting_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and "Starting stage:" in e.get("text", "")
            and "cover_letter" in e.get("text", "")
        ]
        assert starting_events, (
            f"Expected 'log' event with 'Starting stage:' and 'cover_letter'. Got: {calls}"
        )

    async def test_starting_stage_log_event_job_id_matches(self, session):
        """The 'Starting stage' LogEvent has the correct job_id."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        matched = [
            e
            for e in calls
            if e.get("type") == "log"
            and "Starting stage:" in e.get("text", "")
            and e.get("job_id") == job.id
        ]
        assert matched, f"Expected 'Starting stage' log event with job_id={job.id!r}"


# ---------------------------------------------------------------------------
# Point E: LogEvent published when FINAL received
# ---------------------------------------------------------------------------


class TestPointE_FinalReceivedLogEvent:
    async def test_log_event_final_received_published_on_cv_adjust(self, session):
        """run_stage publishes 'FINAL received' after a successful cv_adjust."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        final_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and "FINAL received" in e.get("text", "")
            and "cv_adjust" in e.get("text", "")
        ]
        assert final_events, (
            f"Expected 'log' event with 'FINAL received' and 'cv_adjust'. Got: {calls}"
        )

    async def test_log_event_final_received_published_on_cover_letter(self, session):
        """run_stage publishes 'FINAL received' after a successful cover_letter."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cover_letter, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        final_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and "FINAL received" in e.get("text", "")
            and "cover_letter" in e.get("text", "")
        ]
        assert final_events, (
            f"Expected 'log' event with 'FINAL received' and 'cover_letter'. Got: {calls}"
        )

    async def test_final_received_not_published_on_needs_input(self, session):
        """'FINAL received' is NOT published when the agent returns needs_input."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with pytest.raises(PausedForInput):
                await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        final_events = [
            e for e in calls if "FINAL received" in e.get("text", "")
        ]
        assert not final_events, (
            f"'FINAL received' should not be published on needs_input. Got: {final_events}"
        )


# ---------------------------------------------------------------------------
# Point F: LogEvent published when NEED_INPUT received
# ---------------------------------------------------------------------------


class TestPointF_NeedInputLogEvent:
    async def test_log_event_need_input_published(self, session):
        """run_stage publishes 'NEED_INPUT' log event when agent returns needs_input."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply("What is your role?")])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with pytest.raises(PausedForInput):
                await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        need_input_events = [
            e
            for e in calls
            if e.get("type") == "log"
            and "NEED_INPUT" in e.get("text", "")
            and "cv_adjust" in e.get("text", "")
        ]
        assert need_input_events, (
            f"Expected 'log' event with 'NEED_INPUT' and 'cv_adjust'. Got: {calls}"
        )

    async def test_need_input_log_level_is_info(self, session):
        """The NEED_INPUT log event has level='info'."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with pytest.raises(PausedForInput):
                await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        need_input_events = [
            e for e in calls if "NEED_INPUT" in e.get("text", "")
        ]
        assert need_input_events, "Expected at least one NEED_INPUT log event"
        for e in need_input_events:
            assert e.get("level") == "info", (
                f"Expected NEED_INPUT log event with level='info', got level={e.get('level')!r}"
            )

    async def test_need_input_not_published_on_final(self, session):
        """'NEED_INPUT' log event is NOT published when the agent returns final."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        need_input_events = [
            e for e in calls if "NEED_INPUT" in e.get("text", "")
        ]
        assert not need_input_events, (
            f"'NEED_INPUT' log event should not be published on FINAL. Got: {need_input_events}"
        )

    async def test_need_input_log_event_job_id_matches(self, session):
        """The NEED_INPUT log event has the correct job_id."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with pytest.raises(PausedForInput):
                await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        matched = [
            e
            for e in calls
            if "NEED_INPUT" in e.get("text", "")
            and e.get("job_id") == job.id
        ]
        assert matched, f"Expected NEED_INPUT log event with job_id={job.id!r}"


# ---------------------------------------------------------------------------
# Point D+E ordering: "Starting stage" before "FINAL received"
# ---------------------------------------------------------------------------


class TestPointDE_Ordering:
    async def test_starting_stage_published_before_final_received(self, session):
        """Point D ('Starting stage:') is published before Point E ('FINAL received')."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        log_events = [e for e in calls if e.get("type") == "log"]

        texts = [e.get("text", "") for e in log_events]
        starting_indices = [i for i, t in enumerate(texts) if "Starting stage:" in t]
        final_indices = [i for i, t in enumerate(texts) if "FINAL received" in t]

        assert starting_indices, "Expected at least one 'Starting stage:' log event"
        assert final_indices, "Expected at least one 'FINAL received' log event"

        first_starting = min(starting_indices)
        first_final = min(final_indices)
        assert first_starting < first_final, (
            f"'Starting stage:' (index {first_starting}) should come before "
            f"'FINAL received' (index {first_final}). Log events: {texts}"
        )

    async def test_both_starting_and_final_published_in_single_stage_run(self, session):
        """Both 'Starting stage:' and 'FINAL received' are published for a single cv_adjust run."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]
        texts = [e.get("text", "") for e in calls if e.get("type") == "log"]

        has_starting = any("Starting stage:" in t for t in texts)
        has_final = any("FINAL received" in t for t in texts)
        assert has_starting, f"Missing 'Starting stage:' log event. Got: {texts}"
        assert has_final, f"Missing 'FINAL received' log event. Got: {texts}"


# ---------------------------------------------------------------------------
# Point A ordering: LogEvent "Picked up" appears before StatusChangedEvent
# ---------------------------------------------------------------------------


class TestPointA_Ordering:
    async def test_pickup_log_before_status_changed_event(self, session_factory):
        """Point A: 'Picked up' LogEvent is published before the running StatusChangedEvent."""
        job = await _insert_job(session_factory)

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            orch = Orchestrator(
                db_session_factory=session_factory,
                backend_factory=lambda: FakeAgentBackend([_final_reply()]),
            )
            await _run_orchestrator_until(
                orch, session_factory, [job.id], JobState.review
            )

        calls = [c.args[0] for c in mock_pub.call_args_list]

        # Find the index of the first "Picked up" log event
        pickup_indices = [
            i for i, e in enumerate(calls)
            if e.get("type") == "log" and "Picked up" in e.get("text", "")
        ]
        # Find the index of the status_changed event with to_state="running"
        running_status_indices = [
            i for i, e in enumerate(calls)
            if e.get("type") == "status_changed" and e.get("to_state") == "running"
        ]

        assert pickup_indices, "Expected a 'Picked up' LogEvent to be published"
        assert running_status_indices, "Expected a status_changed event with to_state='running'"

        assert min(pickup_indices) < min(running_status_indices), (
            f"'Picked up' log (index {min(pickup_indices)}) should come before "
            f"status_changed→running (index {min(running_status_indices)}). "
            f"All events: {[e.get('type') for e in calls]}"
        )


# ---------------------------------------------------------------------------
# Point F ordering: LogEvent "NEED_INPUT" appears before FollowUpNeededEvent
# ---------------------------------------------------------------------------


class TestPointF_Ordering:
    async def test_need_input_log_before_follow_up_needed_event(self, session):
        """Point F: 'NEED_INPUT' LogEvent is published before FollowUpNeededEvent."""
        job = await _insert_job_in_session(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply()])

        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            with pytest.raises(PausedForInput):
                await run_stage(job, backend, Stage.cv_adjust, session)

        calls = [c.args[0] for c in mock_pub.call_args_list]

        need_input_indices = [
            i for i, e in enumerate(calls)
            if e.get("type") == "log" and "NEED_INPUT" in e.get("text", "")
        ]
        follow_up_indices = [
            i for i, e in enumerate(calls)
            if e.get("type") == "follow_up_needed"
        ]

        assert need_input_indices, "Expected a 'NEED_INPUT' LogEvent to be published"
        assert follow_up_indices, "Expected a 'follow_up_needed' event to be published"

        assert min(need_input_indices) < min(follow_up_indices), (
            f"'NEED_INPUT' log (index {min(need_input_indices)}) should come before "
            f"follow_up_needed (index {min(follow_up_indices)}). "
            f"All events: {[e.get('type') for e in calls]}"
        )
