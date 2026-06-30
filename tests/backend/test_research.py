"""Unit tests for Phase SA-1: research subagent pre-step.

Covers:
- _gather_research with FakeAgentBackend (non-ClaudeCliBackend) → NONE placeholder
- _gather_research when run_research raises → placeholder (best-effort fallback)
- _build_initial_user_msg places brief first and preserves CV/JD/TIER fields
- run_stage fresh cv_adjust with FakeAgentBackend → user Message contains [INTEL_BRIEF]
- run_stage resume branch → new user Message is the answer only, no fresh [INTEL_BRIEF]
- ClaudeCliBackend.run_research command construction (cwd, timeout, cmd args)

All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.agents.claude_cli import ClaudeCliBackend, _PROJECT_ROOT
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Message, Stage
from jsa.pipeline.stages import (
    PausedForInput,
    _build_initial_user_msg,
    _gather_research,
    _research_placeholder,
    run_stage,
)
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cl_final, cv_final


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


@pytest.fixture
async def session(session_factory):
    """Yield a single AsyncSession for most tests."""
    async with session_factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _job_data(
    job_id: str = "aabbccdd00112233",
    company: str = "Acme",
    role: str = "Engineer",
    link: str = "https://acme.com/job",
    tier: str = "A",
    jd: str = "Job description text",
    jd_hash: str = "hash0000deadbeef",
    cv_text: str = "Curriculum vitae text",
) -> dict:
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


def _make_job(**overrides) -> Job:
    """Create an in-memory Job ORM instance without inserting into the DB."""
    data = _job_data(**overrides)
    job = Job(**data, state=JobState.pending)
    return job


async def _insert_job(session: AsyncSession, **overrides) -> Job:
    """Insert a job using upsert_job and commit."""
    data = _job_data(**overrides)
    job = await repo.upsert_job(session, data)
    await session.commit()
    return job


def _final_reply(content: str = "Adjusted CV") -> AgentReply:
    return cv_final(content)


def _cl_final_reply() -> AgentReply:
    return cl_final()


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


# ---------------------------------------------------------------------------
# Test 1: _gather_research with FakeAgentBackend → NONE placeholder
# ---------------------------------------------------------------------------


class TestGatherResearchFakeBackend:
    """_gather_research routes backends without run_research to the NONE placeholder."""

    async def test_cv_adjust_returns_intel_brief_tag(self):
        """FakeAgentBackend + cv_adjust stage → result contains [INTEL_BRIEF]."""
        job = _make_job()
        backend = FakeAgentBackend([])  # no replies consumed; isinstance check returns early
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" in result

    async def test_cover_letter_returns_company_brief_tag(self):
        """FakeAgentBackend + cover_letter stage → result contains [COMPANY_BRIEF]."""
        job = _make_job()
        backend = FakeAgentBackend([])
        result = await _gather_research(job, backend, Stage.cover_letter)
        assert "[COMPANY_BRIEF]" in result
        assert "NONE" in result

    async def test_cv_adjust_placeholder_has_closing_tag(self):
        """Placeholder is well-formed — has both open and close tags."""
        job = _make_job()
        backend = FakeAgentBackend([])
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[/INTEL_BRIEF]" in result

    async def test_cover_letter_placeholder_has_closing_tag(self):
        """Cover-letter placeholder has matching close tag."""
        job = _make_job()
        backend = FakeAgentBackend([])
        result = await _gather_research(job, backend, Stage.cover_letter)
        assert "[/COMPANY_BRIEF]" in result


# ---------------------------------------------------------------------------
# Test 2: _gather_research when run_research raises → placeholder fallback
# ---------------------------------------------------------------------------


class TestGatherResearchFallbackOnException:
    """When ClaudeCliBackend.run_research raises, _gather_research returns the placeholder."""

    async def test_exception_in_run_research_returns_placeholder_cv_adjust(self, monkeypatch):
        """run_research raising RuntimeError → cv_adjust NONE placeholder returned."""
        job = _make_job()
        backend = ClaudeCliBackend()

        async def _raise(*args, **kwargs):
            raise RuntimeError("subagent crashed")

        monkeypatch.setattr(ClaudeCliBackend, "run_research", _raise)

        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" in result

    async def test_exception_in_run_research_returns_placeholder_cover_letter(self, monkeypatch):
        """run_research raising RuntimeError → cover_letter NONE placeholder returned."""
        job = _make_job()
        backend = ClaudeCliBackend()

        async def _raise(*args, **kwargs):
            raise RuntimeError("network error")

        monkeypatch.setattr(ClaudeCliBackend, "run_research", _raise)

        result = await _gather_research(job, backend, Stage.cover_letter)
        assert "[COMPANY_BRIEF]" in result
        assert "NONE" in result

    async def test_exception_does_not_propagate(self, monkeypatch):
        """_gather_research must not propagate any exception from run_research."""
        job = _make_job()
        backend = ClaudeCliBackend()

        async def _raise(*args, **kwargs):
            raise ValueError("unexpected failure")

        monkeypatch.setattr(ClaudeCliBackend, "run_research", _raise)

        # Must not raise
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert result  # returns something non-empty

    async def test_run_research_returns_text_without_tag_falls_back(self, monkeypatch):
        """If run_research returns text lacking the expected open_tag, fall back to placeholder."""
        job = _make_job()
        backend = ClaudeCliBackend()

        async def _no_tag(*args, **kwargs):
            return "Some research output without the expected tag"

        monkeypatch.setattr(ClaudeCliBackend, "run_research", _no_tag)

        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" in result


# ---------------------------------------------------------------------------
# Test 3: _build_initial_user_msg places brief first, preserves all fields
# ---------------------------------------------------------------------------


class TestBuildInitialUserMsg:
    """_build_initial_user_msg(job, brief) injects brief at the top."""

    def test_brief_appears_before_cv_text(self):
        """The brief must appear before 'CV TEXT:' in the message."""
        job = _make_job(cv_text="My CV here")
        brief = "[INTEL_BRIEF]\nNONE\n[/INTEL_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert msg.index(brief) < msg.index("CV TEXT:")

    def test_brief_appears_before_job_description(self):
        """The brief must appear before 'JOB DESCRIPTION:' in the message."""
        job = _make_job(jd="Job desc here")
        brief = "[INTEL_BRIEF]\nNONE\n[/INTEL_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert msg.index(brief) < msg.index("JOB DESCRIPTION:")

    def test_brief_appears_before_tier(self):
        """The brief must appear before 'TIER:' in the message."""
        job = _make_job(tier="B")
        brief = "[COMPANY_BRIEF]\nNONE\n[/COMPANY_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert msg.index(brief) < msg.index("TIER:")

    def test_cv_text_preserved(self):
        """CV text from the job is present in the message."""
        job = _make_job(cv_text="Alice's full CV")
        brief = "[INTEL_BRIEF]\nNONE\n[/INTEL_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert "Alice's full CV" in msg

    def test_jd_preserved(self):
        """Job description from the job is present in the message."""
        job = _make_job(jd="Senior Python developer needed")
        brief = "[INTEL_BRIEF]\nNONE\n[/INTEL_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert "Senior Python developer needed" in msg

    def test_tier_preserved(self):
        """Tier value from the job is present in the message."""
        job = _make_job(tier="C")
        brief = "[COMPANY_BRIEF]\nNONE\n[/COMPANY_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert "TIER: C" in msg

    def test_all_three_section_headers_present(self):
        """All three headers (CV TEXT, JOB DESCRIPTION, TIER) appear in message."""
        job = _make_job()
        brief = "[INTEL_BRIEF]\nNONE\n[/INTEL_BRIEF]"
        msg = _build_initial_user_msg(job, brief)
        assert "CV TEXT:" in msg
        assert "JOB DESCRIPTION:" in msg
        assert "TIER:" in msg


# ---------------------------------------------------------------------------
# Test 4: run_stage fresh cv_adjust → persisted user Message contains [INTEL_BRIEF]
# ---------------------------------------------------------------------------


class TestRunStageFreshCvAdjustContainsIntelBrief:
    """On a fresh cv_adjust, the persisted user Message must contain the [INTEL_BRIEF] block."""

    async def test_fresh_cv_adjust_user_message_contains_intel_brief(self, session):
        """Fresh cv_adjust with FakeAgentBackend: user Message row has [INTEL_BRIEF]."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# My Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        # Fetch the user message for this stage
        result = await session.execute(
            select(Message).where(
                Message.job_id == job.id,
                Message.stage == Stage.cv_adjust,
                Message.role == "user",
            )
        )
        user_msgs = list(result.scalars().all())
        assert len(user_msgs) == 1
        assert "[INTEL_BRIEF]" in user_msgs[0].content

    async def test_fresh_cover_letter_user_message_contains_company_brief(self, session):
        """Fresh cover_letter with FakeAgentBackend: user Message row has [COMPANY_BRIEF]."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_cl_final_reply()])
        await run_stage(job, backend, Stage.cover_letter, session)

        result = await session.execute(
            select(Message).where(
                Message.job_id == job.id,
                Message.stage == Stage.cover_letter,
                Message.role == "user",
            )
        )
        user_msgs = list(result.scalars().all())
        assert len(user_msgs) == 1
        assert "[COMPANY_BRIEF]" in user_msgs[0].content


# ---------------------------------------------------------------------------
# Test 5: run_stage resume branch → new user Message is answer only, no [INTEL_BRIEF]
# ---------------------------------------------------------------------------


class TestRunStageResumeBranchNoFreshBrief:
    """On resume, _gather_research must not be called; the new user Message is the answer only."""

    async def test_resume_user_message_is_answer_only(self, session):
        """After park+resume, the newly written user Message equals the answer text."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: park with needs_input (this writes the fresh [INTEL_BRIEF] message)
        backend1 = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        # Snapshot message count right after parking
        msgs_result = await session.execute(
            select(Message).where(Message.job_id == job.id)
        )
        msgs_after_park = list(msgs_result.scalars().all())
        count_before_resume = len(msgs_after_park)

        # Answer the follow-up
        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fu = fus[0]
        answer_text = "Software Engineer"
        fu.answer = answer_text
        fu.answered_at = datetime.utcnow()
        await session.commit()

        # Transition back to running for the resume
        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Second run: backend returns FINAL
        backend2 = FakeAgentBackend([_final_reply("# Adjusted CV after answer")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        # Fetch only the newly added messages (ids higher than before resume)
        all_msgs_result = await session.execute(
            select(Message)
            .where(Message.job_id == job.id)
            .order_by(Message.id.asc())
        )
        all_msgs = list(all_msgs_result.scalars().all())
        # Messages added during resume are those beyond the count before resume
        new_msgs = all_msgs[count_before_resume:]
        assert len(new_msgs) == 2  # user answer + assistant reply

        # The newly written user Message is the answer text, NOT a fresh brief
        new_user_msgs = [m for m in new_msgs if m.role == "user"]
        assert len(new_user_msgs) == 1
        assert new_user_msgs[0].content == answer_text
        assert "[INTEL_BRIEF]" not in new_user_msgs[0].content

    async def test_resume_user_message_does_not_contain_intel_brief(self, session):
        """The resume user Message must not contain [INTEL_BRIEF] — research is not re-run."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend1 = FakeAgentBackend([_needs_input_reply("Tell me about the company")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fus[0].answer = "Acme is a tech startup"
        fus[0].answered_at = datetime.utcnow()
        await session.commit()

        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend2 = FakeAgentBackend([_final_reply("# Final CV")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        # The highest-id user message is the resume answer
        result = await session.execute(
            select(Message)
            .where(
                Message.job_id == job.id,
                Message.stage == Stage.cv_adjust,
                Message.role == "user",
            )
            .order_by(Message.id.desc())
        )
        latest_user_msg = result.scalars().first()
        assert latest_user_msg is not None
        # The resume answer should not contain [INTEL_BRIEF]
        assert "[INTEL_BRIEF]" not in latest_user_msg.content
        # And it should be the answer text we provided
        assert "Acme is a tech startup" in latest_user_msg.content


# ---------------------------------------------------------------------------
# Test 6: ClaudeCliBackend.run_research command construction
# ---------------------------------------------------------------------------


class TestRunResearchCommandConstruction:
    """run_research builds the correct subprocess call with cwd=_PROJECT_ROOT and RESEARCH_TIMEOUT."""

    def _make_subprocess_result(self, stdout_text: str) -> SimpleNamespace:
        """Create a fake subprocess.CompletedProcess-like object."""
        return SimpleNamespace(
            returncode=0,
            stdout=stdout_text.encode("utf-8"),
            stderr=b"",
        )

    async def test_run_research_command_args(self):
        """run_research builds: claude --agent <name> --output-format text -p <query>."""
        backend = ClaudeCliBackend()
        agent_name = "cv-research"
        query = "Company: Acme\nRole: Engineer"
        expected_brief = "[INTEL_BRIEF]\nSome research\n[/INTEL_BRIEF]"
        captured_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_calls.append({"cmd": cmd, "kwargs": kwargs})
            return self._make_subprocess_result(expected_brief)

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            result = await backend.run_research(agent_name, query)

        assert len(captured_calls) == 1
        call = captured_calls[0]
        assert call["cmd"] == ["claude", "--agent", agent_name, "--output-format", "text", "-p", query]

    async def test_run_research_cwd_is_project_root(self):
        """run_research passes cwd=str(_PROJECT_ROOT) to subprocess.run."""
        backend = ClaudeCliBackend()
        captured_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(kwargs)
            return self._make_subprocess_result("[INTEL_BRIEF]\ntest\n[/INTEL_BRIEF]")

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            await backend.run_research("cv-research", "test query")

        assert len(captured_calls) == 1
        assert captured_calls[0].get("cwd") == str(_PROJECT_ROOT)

    async def test_run_research_timeout_is_research_timeout(self):
        """run_research passes timeout=RESEARCH_TIMEOUT (300.0) to subprocess.run."""
        backend = ClaudeCliBackend()
        captured_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(kwargs)
            return self._make_subprocess_result("[INTEL_BRIEF]\ntest\n[/INTEL_BRIEF]")

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            await backend.run_research("cv-research", "test query")

        assert len(captured_calls) == 1
        assert captured_calls[0].get("timeout") == ClaudeCliBackend.RESEARCH_TIMEOUT

    async def test_run_research_returns_stdout(self):
        """run_research returns the raw stdout string from the subprocess."""
        backend = ClaudeCliBackend()
        expected_output = "[INTEL_BRIEF]\nCompany: Acme\n[/INTEL_BRIEF]"

        def fake_subprocess_run(cmd, **kwargs):
            return self._make_subprocess_result(expected_output)

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            result = await backend.run_research("cv-research", "test query")

        assert result == expected_output

    async def test_run_research_cl_research_agent(self):
        """run_research uses 'cl-research' agent name when specified."""
        backend = ClaudeCliBackend()
        captured_calls = []
        query = "Company: Acme\nRole: Engineer\nJob posting link: https://acme.com/job"

        def fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(cmd)
            return self._make_subprocess_result("[COMPANY_BRIEF]\ntest\n[/COMPANY_BRIEF]")

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            await backend.run_research("cl-research", query)

        assert len(captured_calls) == 1
        assert "--agent" in captured_calls[0]
        agent_idx = captured_calls[0].index("--agent")
        assert captured_calls[0][agent_idx + 1] == "cl-research"

    async def test_run_research_existing_calls_unaffected_no_cwd(self):
        """Existing start_session calls do NOT pass cwd (cwd=None → cwd=None in subprocess)."""
        backend = ClaudeCliBackend()
        captured_calls = []

        def fake_subprocess_run(cmd, **kwargs):
            captured_calls.append(kwargs)
            return self._make_subprocess_result(
                "<<<FINAL>>>\n# My CV\n<<<END>>>"
            )

        with patch("jsa.agents.claude_cli.subprocess.run", side_effect=fake_subprocess_run):
            # start_session calls _run without cwd
            try:
                await backend.start_session("System prompt", "User message")
            except Exception:
                pass  # parse_reply may not fail, but we only care about cwd

        assert len(captured_calls) >= 1
        # The first call from start_session must NOT pass a cwd (cwd=None means subprocess omits it)
        assert captured_calls[0].get("cwd") is None


# ---------------------------------------------------------------------------
# Test 6.5: _gather_research success path — verbatim brief returned when tag present
# ---------------------------------------------------------------------------


class TestGatherResearchSuccessPath:
    """_gather_research returns the verbatim brief when run_research succeeds and tag is present."""

    async def test_gather_research_returns_verbatim_brief_when_tag_present(self, monkeypatch):
        """When run_research returns text with [INTEL_BRIEF], the verbatim brief is returned."""
        job = _make_job()
        backend = ClaudeCliBackend()

        async def _fake_run_research(*args, **kwargs):
            return "[INTEL_BRIEF]\nsome content\n[/INTEL_BRIEF]\n"

        monkeypatch.setattr(ClaudeCliBackend, "run_research", _fake_run_research)

        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "some content" in result


# ---------------------------------------------------------------------------
# Test: _research_placeholder output shapes
# ---------------------------------------------------------------------------


class TestResearchPlaceholder:
    """Sanity checks on _research_placeholder to validate test assumptions."""

    def test_cv_adjust_tag(self):
        result = _research_placeholder(Stage.cv_adjust)
        assert result.startswith("[INTEL_BRIEF]")
        assert "[/INTEL_BRIEF]" in result
        assert "NONE" in result

    def test_cover_letter_tag(self):
        result = _research_placeholder(Stage.cover_letter)
        assert result.startswith("[COMPANY_BRIEF]")
        assert "[/COMPANY_BRIEF]" in result
        assert "NONE" in result

    def test_revising_cv_uses_intel_brief(self):
        """revising_cv is treated like cv_adjust for placeholder tags."""
        result = _research_placeholder(Stage.revising_cv)
        assert "[INTEL_BRIEF]" in result

    def test_revising_cl_uses_company_brief(self):
        """revising_cl is treated like cover_letter for placeholder tags."""
        result = _research_placeholder(Stage.revising_cl)
        assert "[COMPANY_BRIEF]" in result


# ---------------------------------------------------------------------------
# Test SA-2: _gather_research dispatches to GoogleCliBackend.run_research
# ---------------------------------------------------------------------------


class TestGatherResearchGeminiBackend:
    """_gather_research dispatches to GoogleCliBackend.run_research when it returns a valid brief."""

    async def test_cv_adjust_returns_real_brief(self, monkeypatch):
        """GoogleCliBackend.run_research returns valid brief → _gather_research returns it (not NONE)."""
        from jsa.agents.google_cli import GoogleCliBackend
        job = _make_job()
        backend = GoogleCliBackend()

        async def _fake_research(self, agent_name, query):
            return "[INTEL_BRIEF]\nCompany: Acme\n[/INTEL_BRIEF]"

        monkeypatch.setattr(GoogleCliBackend, "run_research", _fake_research)
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" not in result

    async def test_cover_letter_returns_real_brief(self, monkeypatch):
        from jsa.agents.google_cli import GoogleCliBackend
        job = _make_job()
        backend = GoogleCliBackend()

        async def _fake_research(self, agent_name, query):
            return "[COMPANY_BRIEF]\nWhat they do: Makes widgets\n[/COMPANY_BRIEF]"

        monkeypatch.setattr(GoogleCliBackend, "run_research", _fake_research)
        result = await _gather_research(job, backend, Stage.cover_letter)
        assert "[COMPANY_BRIEF]" in result
        assert "NONE" not in result

    async def test_run_research_failure_returns_placeholder(self, monkeypatch):
        """If GoogleCliBackend.run_research raises, _gather_research returns placeholder."""
        from jsa.agents.google_cli import GoogleCliBackend
        job = _make_job()
        backend = GoogleCliBackend()

        async def _raise(self, agent_name, query):
            raise RuntimeError("gemini quota exceeded")

        monkeypatch.setattr(GoogleCliBackend, "run_research", _raise)
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" in result

    async def test_run_research_missing_tag_returns_placeholder(self, monkeypatch):
        """If run_research returns text without the expected open_tag, fall back to placeholder."""
        from jsa.agents.google_cli import GoogleCliBackend
        job = _make_job()
        backend = GoogleCliBackend()

        async def _no_tag(self, agent_name, query):
            return "Some research output without the expected tag"

        monkeypatch.setattr(GoogleCliBackend, "run_research", _no_tag)
        result = await _gather_research(job, backend, Stage.cv_adjust)
        assert "[INTEL_BRIEF]" in result
        assert "NONE" in result


# ---------------------------------------------------------------------------
# Test SA-2: GoogleCliBackend.run_research command construction
# ---------------------------------------------------------------------------


class TestGoogleRunResearch:
    """GoogleCliBackend.run_research command construction and response extraction."""

    async def test_cv_research_calls_agy_with_prompt(self, monkeypatch):
        """run_research for cv-research embeds the system prompt and query in -p."""
        from jsa.agents.google_cli import GoogleCliBackend
        backend = GoogleCliBackend()
        captured_cmd = []

        def _fake_run(cmd, context="", timeout=None, log_path=None):
            captured_cmd.extend(cmd)
            return {"response": "[INTEL_BRIEF]\nCompany: Acme\n[/INTEL_BRIEF]"}

        monkeypatch.setattr(backend, "_run", _fake_run)
        result = await backend.run_research("cv-research", "Company: Acme\nRole: Engineer")

        assert "agy" in captured_cmd
        assert "--dangerously-skip-permissions" in captured_cmd
        assert "-p" in captured_cmd
        assert result == "[INTEL_BRIEF]\nCompany: Acme\n[/INTEL_BRIEF]"

    async def test_cl_research_calls_gemini_with_prompt(self, monkeypatch):
        """run_research for cl-research returns data["response"]."""
        from jsa.agents.google_cli import GoogleCliBackend
        backend = GoogleCliBackend()

        def _fake_run(cmd, context="", timeout=None):
            return {"response": "[COMPANY_BRIEF]\nWhat they do: Makes widgets\n[/COMPANY_BRIEF]"}

        monkeypatch.setattr(backend, "_run", _fake_run)
        result = await backend.run_research("cl-research", "Company: Acme\nRole: Engineer")
        assert result == "[COMPANY_BRIEF]\nWhat they do: Makes widgets\n[/COMPANY_BRIEF]"

    async def test_run_research_uses_research_timeout(self, monkeypatch):
        """run_research passes RESEARCH_TIMEOUT (300s), not the default session timeout."""
        from jsa.agents.google_cli import GoogleCliBackend
        backend = GoogleCliBackend(timeout=10.0)  # short session timeout
        captured_timeout = []

        def _fake_run(cmd, context="", timeout=None):
            captured_timeout.append(timeout)
            return {"response": "[INTEL_BRIEF]\nfoo\n[/INTEL_BRIEF]"}

        monkeypatch.setattr(backend, "_run", _fake_run)
        await backend.run_research("cv-research", "query")
        assert captured_timeout[0] == GoogleCliBackend.RESEARCH_TIMEOUT

    async def test_run_research_unknown_agent_raises(self):
        """run_research raises ValueError for unknown agent names."""
        from jsa.agents.google_cli import GoogleCliBackend
        backend = GoogleCliBackend()
        with pytest.raises(ValueError, match="Unknown research agent"):
            await backend.run_research("unknown-agent", "query")
