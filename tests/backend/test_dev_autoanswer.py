"""Tests for jsa.dev: DevAutoResponder, match_answer, load_rules.

Tests follow the fakes-over-mocks convention (FakeAgentBackend).
All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Stage
from jsa.dev.answers import Rules, load_rules, match_answer
from jsa.dev.autoresponder import DevAutoResponder
from jsa.pipeline.orchestrator import Orchestrator
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend


# ---------------------------------------------------------------------------
# Fixtures
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
def rules_path(tmp_path: Path) -> Path:
    path = tmp_path / "DEV_ANSWERS.json"
    path.write_text(
        json.dumps(
            {
                "default": "Default answer.",
                "rules": [
                    {"match": "strategy look accurate", "answer": "Strategy answer."},
                    {"match": "motivation", "answer": "Motivation answer."},
                ],
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# Helpers
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
        cv_text="CV text here",
    )


async def _insert_job(factory) -> Job:
    async with factory() as s:
        job = await repo.upsert_job(s, _job_data())
        await s.commit()
    return job


def _fit_reply() -> AgentReply:
    return AgentReply(raw="<<<FINAL>>>\nFIT\n<<<END>>>", content="FIT", kind="final")


def _needs_input_reply(question: str = "Does the strategy look accurate?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


def _final_cv() -> AgentReply:
    content = (
        "# John Doe | john@example.com | github.com/john\n\n"
        "---\n\n"
        "## Professional Experience\n\n"
        "**Senior Software Engineer — Acme Corp (2020–present)**\n"
        "Led backend systems design, built distributed data pipelines processing 10M "
        "events/day, and mentored a team of five junior engineers. Drove a 40% "
        "latency reduction across core API services through targeted profiling.\n\n"
        "**Software Engineer — Beta Ltd (2017–2020)**\n"
        "Designed and maintained REST APIs for a SaaS platform with 500k monthly "
        "active users. Improved CI/CD pipeline throughput by 60% and collaborated "
        "cross-functionally on quarterly delivery cycles.\n\n"
        "## Education\n\n"
        "**BSc Computer Science** — University of Example (2013–2017). First-class.\n\n"
        "## Skills\n\nPython, Go, SQL, PostgreSQL, Docker, Kubernetes, AWS, Git."
    )
    assert len(content) >= 300, f"CV content too short: {len(content)}"
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _final_cl() -> AgentReply:
    content = (
        "Dear Hiring Manager,\n\n"
        "I am writing to express my strong interest in the Engineer position at Acme. "
        "Having worked in this domain for several years, I am confident that my skills "
        "and experience make me an excellent fit for this role. I look forward to "
        "discussing how I can contribute to your team.\n\nBest regards,\nJohn Doe"
    )
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


async def _poll_job_state(
    factory,
    job_id: str,
    target_state: JobState,
    timeout: float = 5.0,
) -> Job:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        async with factory() as s:
            job = await repo.get_job(s, job_id)
        if job is not None and job.state == target_state:
            return job
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError(
                f"Job {job_id} did not reach {target_state.value!r} within {timeout}s "
                f"(current: {job.state.value if job else 'None'})"
            )
        await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# Unit tests: match_answer
# ---------------------------------------------------------------------------


class TestMatchAnswer:
    def _rules(self) -> Rules:
        return Rules(
            default="Default.",
            rules=[
                {"match": "strategy look accurate", "answer": "Strategy answer."},
                {"match": "motivation", "answer": "Motivation answer."},
                {"match": "skills", "answer": "Skills answer."},
            ],
        )

    def test_substring_hit(self):
        rules = self._rules()
        assert match_answer("Does the strategy look accurate?", rules) == "Strategy answer."

    def test_case_insensitive(self):
        rules = self._rules()
        assert match_answer("MOTIVATION for applying", rules) == "Motivation answer."
        assert match_answer("what is your MOTIVATION", rules) == "Motivation answer."

    def test_first_rule_wins(self):
        # Both "strategy look accurate" and "skills" could match if question contained both.
        # Verify ordering: first rule whose match appears wins.
        rules = Rules(
            default="Default.",
            rules=[
                {"match": "first", "answer": "First answer."},
                {"match": "second", "answer": "Second answer."},
            ],
        )
        assert match_answer("first and second in question", rules) == "First answer."

    def test_default_fallback(self):
        rules = self._rules()
        assert match_answer("Unrecognised question here", rules) == "Default."

    def test_empty_rules_list_returns_default(self):
        rules = Rules(default="Only default.", rules=[])
        assert match_answer("anything", rules) == "Only default."


# ---------------------------------------------------------------------------
# Unit tests: load_rules
# ---------------------------------------------------------------------------


class TestLoadRules:
    def test_reads_from_disk(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps({"default": "D", "rules": [{"match": "foo", "answer": "bar"}]})
        )
        rules = load_rules(path)
        assert rules["default"] == "D"
        assert rules["rules"][0]["match"] == "foo"
        assert rules["rules"][0]["answer"] == "bar"

    def test_no_caching_reflects_edits(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"default": "old", "rules": []}))
        assert load_rules(path)["default"] == "old"
        # Edit the file
        path.write_text(json.dumps({"default": "new", "rules": []}))
        assert load_rules(path)["default"] == "new"

    def test_empty_rules_key_is_optional(self, tmp_path: Path):
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"default": "D"}))
        rules = load_rules(path)
        assert rules["default"] == "D"
        assert rules["rules"] == []


# ---------------------------------------------------------------------------
# Integration test: end-to-end auto-answer to review
# ---------------------------------------------------------------------------


class TestDevAutoResponderE2E:
    async def test_job_reaches_review_without_manual_answer(
        self, session_factory, rules_path
    ):
        """DevAutoResponder's startup scan answers a pre-parked job and it advances to review.

        Strategy:
        1. Run an orchestrator that parks the job in awaiting_input (deterministic).
        2. Stop that orchestrator.
        3. Start a fresh orchestrator + DevAutoResponder.
           The responder's startup scan finds the parked followup and answers it.
        4. Job advances to review with no manual /answer call.
        """
        job = await _insert_job(session_factory)

        # Phase 1: park the job at awaiting_input
        backend_park = FakeAgentBackend([
            _fit_reply(),
            _needs_input_reply("Does the strategy look accurate?"),
        ])
        orch_park = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_park,
        )
        park_task = asyncio.create_task(orch_park.run())
        await _poll_job_state(session_factory, job.id, JobState.awaiting_input)
        orch_park._stopping = True
        orch_park.kick()
        await asyncio.wait_for(park_task, timeout=5.0)

        # Phase 2: fresh orchestrator + responder; startup scan handles the parked job
        backend_resume = FakeAgentBackend([_final_cv(), _final_cl()])
        orch_resume = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend_resume,
        )
        responder = DevAutoResponder(session_factory, orch_resume, rules_path)

        resume_task = asyncio.create_task(orch_resume.run())
        responder_task = asyncio.create_task(responder.run())
        try:
            await _poll_job_state(session_factory, job.id, JobState.review)
        finally:
            orch_resume._stopping = True
            responder._stopping = True
            orch_resume.kick()
            await asyncio.wait_for(resume_task, timeout=5.0)
            await asyncio.wait_for(responder_task, timeout=5.0)

        # Verify the FollowUp was answered with the matched text
        from sqlalchemy import select as _select
        async with session_factory() as s:
            result = await s.execute(
                _select(FollowUp).where(FollowUp.job_id == job.id)
            )
            fus = list(result.scalars().all())
        assert any(fu.answer == "Strategy answer." for fu in fus)
        assert all(fu.answered_at is not None for fu in fus)


# ---------------------------------------------------------------------------
# Unit test: cap prevents runaway answering
# ---------------------------------------------------------------------------


class TestDevAutoResponderCap:
    async def test_cap_blocks_further_answers(self, session_factory, rules_path):
        """After the cap is reached, _answer_followup returns False."""
        job = await _insert_job(session_factory)
        # Walk through valid transitions to reach awaiting_input
        async with session_factory() as s:
            j = await repo.get_job(s, job.id)
            transition(j, JobState.running, Stage.cv_adjust)
            transition(j, JobState.awaiting_input, Stage.cv_adjust)
            s.add(j)
            fu = FollowUp(
                job_id=j.id,
                stage=Stage.cv_adjust,
                question="Does the strategy look accurate?",
            )
            s.add(fu)
            await s.commit()
            fu_id = fu.id

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([]),
        )
        responder = DevAutoResponder(session_factory, orch, rules_path, cap=2)

        # Fill the counter to cap
        responder._counts[(job.id, Stage.cv_adjust.value)] = 2

        result = await responder._answer_followup(
            job.id, fu_id, "Does the strategy look accurate?", Stage.cv_adjust.value
        )
        assert result is False

        # FollowUp remains unanswered
        async with session_factory() as s:
            fu_row = await s.get(FollowUp, fu_id)
        assert fu_row.answered_at is None

    async def test_cap_not_reached_answers_normally(self, session_factory, rules_path):
        """Before the cap, _answer_followup returns True and fills the FollowUp."""
        job = await _insert_job(session_factory)
        async with session_factory() as s:
            j = await repo.get_job(s, job.id)
            transition(j, JobState.running, Stage.cv_adjust)
            transition(j, JobState.awaiting_input, Stage.cv_adjust)
            s.add(j)
            fu = FollowUp(
                job_id=j.id,
                stage=Stage.cv_adjust,
                question="Motivation?",
            )
            s.add(fu)
            await s.commit()
            fu_id = fu.id

        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: FakeAgentBackend([]),
        )
        responder = DevAutoResponder(session_factory, orch, rules_path, cap=2)

        result = await responder._answer_followup(
            job.id, fu_id, "Motivation?", Stage.cv_adjust.value
        )
        assert result is True

        async with session_factory() as s:
            fu_row = await s.get(FollowUp, fu_id)
        assert fu_row.answered_at is not None
        assert fu_row.answer == "Motivation answer."


# ---------------------------------------------------------------------------
# Negative test: without DevAutoResponder, job stays parked
# ---------------------------------------------------------------------------


class TestNoAutoResponder:
    async def test_job_stays_parked_without_responder(self, session_factory):
        """Without DevAutoResponder, a needs_input job parks and stays in awaiting_input."""
        job = await _insert_job(session_factory)

        backend = FakeAgentBackend([_fit_reply(), _needs_input_reply("Strategy?")])
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=lambda: backend,
        )

        orch_task = asyncio.create_task(orch.run())
        try:
            await asyncio.wait_for(
                _poll_job_state(session_factory, job.id, JobState.awaiting_input),
                timeout=5.0,
            )
            # Give the orchestrator extra time — it should NOT advance further
            await asyncio.sleep(0.3)
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(orch_task, timeout=5.0)

        async with session_factory() as s:
            still_parked = await repo.get_job(s, job.id)
        assert still_parked.state == JobState.awaiting_input
