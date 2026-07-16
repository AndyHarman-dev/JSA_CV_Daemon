"""Tests for U6: backend-switch history retention + rate-limit backoff.

Covers two related changes to the AgentLimitReached handling path:

1. jsa.db.repo.backend_switch_reset now RETAINS a failed stage's Message
   history (instead of unconditionally deleting it) when the new backend is
   "history-capable" (jsa.agents.registry.supports_history_replay) — i.e. can
   reconstruct a session purely from a replayed `history` list, with no
   dependency on a backend-native external session id. AnthropicAPIBackend is
   history-capable; CLI backends (claude-cli, google-cli) are not.

2. jsa.pipeline.orchestrator.Orchestrator now retries an AgentLimitReached on
   the SAME backend with bounded exponential backoff before falling through to
   the existing backend-switch/mark-failed logic, and paces concurrent
   provider calls to a single backend via a shared per-backend semaphore
   (separate from the job-slot semaphore).

Uses FakeAgentBackend (extended here with `raise_limit_times` and
`supports_history_replay`) and in-memory SQLite, per CLAUDE.md's "fakes over
mocks" testing convention.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents import registry as agent_registry
from jsa.agents.base import AgentReply, HistoryTurn
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Message, Stage
from jsa.pipeline import stages
from jsa.pipeline.orchestrator import Orchestrator
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


# ---------------------------------------------------------------------------
# Fixtures / helpers
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
def history_capable_backend_name():
    """Temporarily register a fake "history-capable" backend class (mirrors
    AnthropicAPIBackend) under a test-only name, so
    jsa.agents.registry.supports_history_replay resolves it the same way
    repo.backend_switch_reset does in production. Unregistered afterward."""
    name = f"fake-history-capable-{uuid4().hex[:8]}"

    class _HistoryCapableFake(FakeAgentBackend):
        supports_history_replay = True

        def __init__(self) -> None:
            super().__init__([])

    agent_registry.register(name, _HistoryCapableFake)
    yield name
    agent_registry._REGISTRY.pop(name, None)


def _job_data(job_id: str | None = None, **overrides) -> dict:
    data = dict(
        id=job_id or uuid4().hex[:16],
        company="Acme",
        role="Engineer",
        link="https://acme.com/job",
        tier="A",
        jd="Job description text",
        jd_hash="hash0000deadbeef",
        cv_text="Curriculum vitae text",
    )
    data.update(overrides)
    return data


async def _insert_running_job(
    factory,
    *,
    stage: Stage,
    backend_name: str = "claude-cli",
    **overrides,
) -> Job:
    """Insert a job already `running` at `stage` (mimicking mid-dispatch state)."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.running
        job.current_stage = stage
        job.backend_name = backend_name
        await s.commit()
        await s.refresh(job)
    return job


async def _add_message(factory, job_id: str, stage: Stage, role: str, content: str) -> None:
    async with factory() as s:
        s.add(Message(job_id=job_id, stage=stage, role=role, content=content))
        await s.commit()


async def _add_answered_followup(
    factory, job_id: str, stage: Stage, question: str, answer: str
) -> None:
    async with factory() as s:
        s.add(
            FollowUp(
                job_id=job_id,
                stage=stage,
                question=question,
                answer=answer,
                answered_at=datetime.utcnow(),
            )
        )
        await s.commit()


async def _messages_for(factory, job_id: str, stage: Stage) -> list[Message]:
    async with factory() as s:
        result = await s.execute(
            select(Message).where(Message.job_id == job_id, Message.stage == stage)
        )
        return list(result.scalars().all())


def _cv_json_content() -> str:
    """A minimal, schema-valid CVDocument JSON payload (with a Summary section,
    so the self-heal loop's soft summary-nudge doesn't fire and consume an
    extra scripted reply) — FINAL payloads must be JSON, not Markdown prose
    (see jsa.pipeline.stages._parse_structured)."""
    return json.dumps(
        {
            "contact": {"name": "Jane Doe", "email": "jane@example.com"},
            "sections": [
                {"name": "Summary", "text": "Experienced engineer tailored for this role."}
            ],
        }
    )


async def _followups_for(factory, job_id: str, stage: Stage) -> list[FollowUp]:
    async with factory() as s:
        result = await s.execute(
            select(FollowUp).where(FollowUp.job_id == job_id, FollowUp.stage == stage)
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Part A: backend_switch_reset — history-capable target retains Messages
# ---------------------------------------------------------------------------


class TestBackendSwitchResetRetainsHistoryWhenCapable:
    async def test_cv_adjust_retains_messages_and_rewinds_to_fit_done(
        self, session_factory, history_capable_backend_name
    ):
        """A cv_adjust limit hit, switching to a history-capable backend, keeps
        the stage's Messages and rewinds to fit_done (NOT pending) so the next
        dispatch maps back to cv_adjust with the retained history intact."""
        job = await _insert_running_job(session_factory, stage=Stage.cv_adjust)
        await _add_message(session_factory, job.id, Stage.cv_adjust, "system", "sys prompt")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "user", "initial msg")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "assistant", "first reply")
        await _add_answered_followup(
            session_factory, job.id, Stage.cv_adjust, "Which template?", "Template B"
        )

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(
                s, db_job, history_capable_backend_name, Stage.cv_adjust
            )

        messages = await _messages_for(session_factory, job.id, Stage.cv_adjust)
        assert len(messages) == 3, "Messages must be RETAINED for a history-capable switch"

        followups = await _followups_for(session_factory, job.id, Stage.cv_adjust)
        assert len(followups) == 1
        assert followups[0].answered_at is not None

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.fit_done
        assert refreshed.current_stage is None
        assert refreshed.session_external_id is None
        assert refreshed.backend_name == history_capable_backend_name

    async def test_cover_letter_retains_messages_and_stays_cv_done(
        self, session_factory, history_capable_backend_name
    ):
        job = await _insert_running_job(session_factory, stage=Stage.cover_letter)
        await _add_message(session_factory, job.id, Stage.cv_adjust, "user", "cv init")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "assistant", "cv final")
        await _add_message(session_factory, job.id, Stage.cover_letter, "user", "cl init")
        await _add_message(session_factory, job.id, Stage.cover_letter, "assistant", "cl reply")

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(
                s, db_job, history_capable_backend_name, Stage.cover_letter
            )

        cl_messages = await _messages_for(session_factory, job.id, Stage.cover_letter)
        assert len(cl_messages) == 2, "cover_letter Messages must be RETAINED"
        cv_messages = await _messages_for(session_factory, job.id, Stage.cv_adjust)
        assert len(cv_messages) == 2, "cv_adjust Messages were never touched by this branch"

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.cv_done

    async def test_revising_cv_retains_messages_and_followups(
        self, session_factory, history_capable_backend_name
    ):
        job = await _insert_running_job(session_factory, stage=Stage.revising_cv)
        await _add_message(session_factory, job.id, Stage.revising_cv, "user", "revise this")
        await _add_message(session_factory, job.id, Stage.revising_cv, "assistant", "question?")
        await _add_answered_followup(
            session_factory, job.id, Stage.revising_cv, "question?", "answer!"
        )

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(
                s, db_job, history_capable_backend_name, Stage.revising_cv
            )

        messages = await _messages_for(session_factory, job.id, Stage.revising_cv)
        assert len(messages) == 2, "revising_cv Messages must be RETAINED"
        followups = await _followups_for(session_factory, job.id, Stage.revising_cv)
        assert len(followups) == 1, "revising_cv FollowUps (incl. answered) must be RETAINED"

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage == Stage.revising_cv


# ---------------------------------------------------------------------------
# Part A regression: non-history-capable target keeps the ORIGINAL full reset
# ---------------------------------------------------------------------------


class TestBackendSwitchResetFullResetWhenNotCapable:
    """Uses the real 'claude-cli' name (registered with supports_history_replay
    left at its default False) to prove the pre-existing full-reset behavior
    is byte-for-byte unchanged for CLI-style targets."""

    async def test_cv_adjust_deletes_messages_and_rewinds_to_pending(self, session_factory):
        job = await _insert_running_job(session_factory, stage=Stage.cv_adjust)
        await _add_message(session_factory, job.id, Stage.cv_adjust, "user", "initial msg")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "assistant", "first reply")

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(s, db_job, "google-cli", Stage.cv_adjust)

        messages = await _messages_for(session_factory, job.id, Stage.cv_adjust)
        assert messages == [], "Messages must be DELETED for a non-history-capable switch"

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.pending

    async def test_cover_letter_deletes_cl_messages_keeps_cv_messages(self, session_factory):
        job = await _insert_running_job(session_factory, stage=Stage.cover_letter)
        await _add_message(session_factory, job.id, Stage.cv_adjust, "assistant", "cv final")
        await _add_message(session_factory, job.id, Stage.cover_letter, "assistant", "cl reply")

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(s, db_job, "google-cli", Stage.cover_letter)

        assert await _messages_for(session_factory, job.id, Stage.cover_letter) == []
        assert len(await _messages_for(session_factory, job.id, Stage.cv_adjust)) == 1

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.cv_done

    async def test_revising_cv_deletes_messages_and_all_followups(self, session_factory):
        job = await _insert_running_job(session_factory, stage=Stage.revising_cv)
        await _add_message(session_factory, job.id, Stage.revising_cv, "assistant", "question?")
        await _add_answered_followup(
            session_factory, job.id, Stage.revising_cv, "question?", "answer!"
        )

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await repo.backend_switch_reset(s, db_job, "google-cli", Stage.revising_cv)

        assert await _messages_for(session_factory, job.id, Stage.revising_cv) == []
        assert await _followups_for(session_factory, job.id, Stage.revising_cv) == []

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage == Stage.revising_cv


# ---------------------------------------------------------------------------
# Part A, dispatch-level: retained history makes run_stage resume (not restart)
# ---------------------------------------------------------------------------


class _CallTrackingBackend(FakeAgentBackend):
    """Spy subclass (still a fake, per CLAUDE.md's fakes-over-mocks rule) that
    records which of start_session/restore_session was invoked, and with what
    history, so a test can assert the dispatch-time branch choice directly."""

    def __init__(self, replies):
        super().__init__(replies)
        self.start_session_called = False
        self.restore_session_history: list[HistoryTurn] | None = None

    async def start_session(self, system_prompt, initial_user_msg):
        self.start_session_called = True
        return await super().start_session(system_prompt, initial_user_msg)

    async def restore_session(self, system_prompt, history, external_id):
        self.restore_session_history = list(history)
        return await super().restore_session(system_prompt, history, external_id)


class TestRunStageResumesFromRetainedHistory:
    async def test_cv_adjust_with_retained_history_calls_restore_not_start(
        self, session_factory
    ):
        """Simulates the post-retention state: Messages exist for cv_adjust and
        an answered FollowUp is in place (as backend_switch_reset would leave
        them for a history-capable target). run_stage's OWN existing
        history-based branch selection (unmodified by this unit) must then
        naturally resume via restore_session + send_message instead of
        starting fresh — proving the retention actually pays off end-to-end.
        """
        job = await _insert_running_job(session_factory, stage=Stage.cv_adjust)
        await _add_message(session_factory, job.id, Stage.cv_adjust, "system", "sys prompt")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "user", "initial msg")
        await _add_message(session_factory, job.id, Stage.cv_adjust, "assistant", "first reply")
        await _add_answered_followup(
            session_factory, job.id, Stage.cv_adjust, "Which template?", "Template B"
        )

        content = _cv_json_content()
        final_reply = AgentReply(
            raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
            content=content,
            kind="final",
        )
        backend = _CallTrackingBackend([final_reply])

        async with session_factory() as s:
            db_job = await repo.get_job(s, job.id)
            await stages.run_stage(
                db_job, backend, Stage.cv_adjust, s,
                output_dir=None, cv_structure_path=None, preferences_path=None,
            )

        assert backend.start_session_called is False, (
            "Retained history must drive a RESUME (restore_session), not a fresh start"
        )
        assert backend.restore_session_history is not None
        # _load_history excludes "system" role rows by design — only the
        # user+assistant turns (2 of the 3 retained Messages) are replayed.
        assert len(backend.restore_session_history) == 2

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.cv_done


# ---------------------------------------------------------------------------
# Part B: backoff/retry on the SAME backend before switching
#
# These tests deliberately drive Orchestrator._run_one() directly for a
# SINGLE dispatch, rather than running the full Orchestrator.run() loop and
# polling for a state several stages downstream. Letting the loop
# auto-continue (cv_done is itself a runnable state -> cover_letter gets
# picked up immediately) was found to occasionally hang/race even on
# unmodified `main` with zero of this unit's changes and a zero-latency fake
# backend (confirmed by reproducing it after a clean `git checkout HEAD --
# jsa/pipeline/orchestrator.py`) — a pre-existing Orchestrator/session
# concurrency issue unrelated to backend-switch retention or backoff, out of
# scope for this unit (CLAUDE.md: fix no pre-existing debt). Driving a single
# `_run_one` call keeps these tests hermetic to exactly the retry/backoff
# logic under test, with no dependency on that separate, pre-existing issue.
# ---------------------------------------------------------------------------


def _cv_final_reply() -> AgentReply:
    content = _cv_json_content()
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


async def _insert_fit_done_job(factory, **overrides) -> Job:
    """Insert a job already past fit_assessment (state=fit_done)."""
    data = _job_data(**overrides)
    async with factory() as s:
        job = await repo.upsert_job(s, data)
        job.state = JobState.fit_done
        await s.commit()
    return job


async def _dispatch_once(
    orch: Orchestrator, factory, job_id: str, stage: Stage, backend_name: str = "claude-cli"
) -> None:
    """Transition job to running(stage) (mimicking what orch.run()'s dispatch
    loop does before spawning a worker) then invoke _run_one for exactly ONE
    dispatch — no loop, no auto-continuation to a later stage."""
    async with factory() as s:
        job = await repo.get_job(s, job_id)
        job.state = JobState.running
        job.current_stage = stage
        job.backend_name = backend_name
        await s.commit()
    await orch._run_one(job_id)


# Tiny retry delays so these tests never depend on real multi-second sleeps.
_FAST_RETRY_KWARGS = dict(
    limit_retry_max_attempts=3,
    limit_retry_base_delay=0.001,
    limit_retry_max_delay=0.01,
)


class TestBackoffRetrySameBackend:
    async def test_limit_reached_twice_then_succeeds_without_switching(self, session_factory):
        """Backend raises AgentLimitReached twice, succeeds on the 3rd call —
        cv_adjust completes on the SAME backend; backend_switch_reset is never
        invoked (backend_name stays whatever it was assigned initially)."""
        job = await _insert_fit_done_job(session_factory)
        backend = FakeAgentBackend([_cv_final_reply()], raise_limit_times=2)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: backend,
            backends=["claude-cli"],
            **_FAST_RETRY_KWARGS,
        )

        await _dispatch_once(orch, session_factory, job.id, Stage.cv_adjust)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.cv_done
        assert refreshed.backend_name == "claude-cli", "no backend switch should have occurred"

    async def test_backoff_exhausted_falls_back_to_existing_switch_then_fail(
        self, session_factory
    ):
        """Regression: when a backend NEVER recovers, backoff retries exhaust
        and the pre-existing chain-exhausted → mark_failed behavior still
        applies unchanged (single-backend chain here)."""
        job = await _insert_fit_done_job(session_factory)
        backend = FakeAgentBackend([], raise_limit_times=10_000)

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda name: backend,
            backends=["claude-cli"],
            **_FAST_RETRY_KWARGS,
        )

        await _dispatch_once(orch, session_factory, job.id, Stage.cv_adjust)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        assert refreshed.state == JobState.failed
        assert "Backend limit reached" in (refreshed.error or "")

    async def test_backoff_exhausted_then_switches_to_next_backend(self, session_factory):
        """Regression: with a 2-backend chain, exhausting backoff on the first
        backend still triggers the existing switch behavior (unchanged) —
        job.backend_name updates to the next backend and the job rewinds per
        backend_switch_reset's existing (non-history-capable) full-reset path.
        """
        job = await _insert_fit_done_job(session_factory)
        limited_backend = FakeAgentBackend([], raise_limit_times=10_000)
        healthy_backend = FakeAgentBackend([])  # never actually called in this dispatch

        def factory(name: str):
            return limited_backend if name == "claude-cli" else healthy_backend

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=factory,
            backends=["claude-cli", "google-cli"],
            **_FAST_RETRY_KWARGS,
        )

        await _dispatch_once(orch, session_factory, job.id, Stage.cv_adjust)

        async with session_factory() as s:
            refreshed = await repo.get_job(s, job.id)
        # google-cli is not history-capable and no Messages existed yet for
        # this fresh cv_adjust attempt, so backend_switch_reset's existing
        # (unmodified) full-reset path rewinds to pending — see repo.py.
        assert refreshed.state == JobState.pending
        assert refreshed.backend_name == "google-cli"
