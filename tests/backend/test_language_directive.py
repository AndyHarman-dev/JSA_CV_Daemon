"""Pipeline language directive (Workstream B of the language-preference feature).

Verifies: the directive is injected into NEW sessions (fresh cv_adjust/cover_letter,
fit_assessment) but never into RESUMED sessions (awaiting_input resume, revisions) — and
that the fit_assessment carve-out (verdict word stays English) is present there.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, JobState, Stage
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle


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


def _job_data(job_id: str = "aabbccdd00112233", **overrides) -> dict:
    data = dict(
        id=job_id,
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


async def _insert_job(session: AsyncSession, **overrides):
    job = await repo.upsert_job(session, _job_data(**overrides))
    job.state = JobState.pending  # simulate an already-launched job
    await session.commit()
    return job


def _cv_json(marker: str = "Adjusted CV") -> dict:
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [
            {"name": "Summary", "text": f"{marker}: senior engineer."},
        ],
    }


def _raw_final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


def _final_reply(marker: str = "Adjusted CV") -> AgentReply:
    return _raw_final(json.dumps(_cv_json(marker)))


def _fit_reply(verdict: str = "FIT", reason: str = "good match") -> AgentReply:
    content = f"{verdict}\n{reason}"
    return _raw_final(content)


class _CapturingBackend(FakeAgentBackend):
    """Records the system_prompt passed to start_session AND restore_session, so a test
    can assert the language directive is present on fresh sessions and absent on resumes."""

    def __init__(self, replies):
        super().__init__(replies)
        self.start_session_prompt: str | None = None
        self.restore_session_prompt: str | None = None

    async def start_session(self, system_prompt, initial_user_msg):
        self.start_session_prompt = system_prompt
        handle = FakeSessionHandle(id=str(uuid4()), external_id=None)
        return handle, self._pop_reply()

    async def restore_session(self, system_prompt, history, external_id):
        self.restore_session_prompt = system_prompt
        return FakeSessionHandle(id=str(uuid4()), external_id=external_id)


def _write_prefs(tmp_path, language: str):
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"language": language}), encoding="utf-8")
    return path


class TestFreshSessionGetsDirective:
    async def test_cv_adjust_fresh_session_carries_spanish_directive(self, session, tmp_path):
        prefs_path = _write_prefs(tmp_path, "es")
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session, preferences_path=prefs_path)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert "## Output language" in prompt
        assert "Spanish (es)" in prompt
        assert "<<<FINAL>>>" in prompt  # sentinel carve-out present

    async def test_default_english_adds_no_directive(self, session, tmp_path):
        # No preferences_path at all → defaults to "en" → directive is a no-op.
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert "## Output language" not in backend.start_session_prompt

    async def test_fit_assessment_fresh_session_has_verdict_carveout(self, session, tmp_path):
        prefs_path = _write_prefs(tmp_path, "ja")
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        backend = _CapturingBackend([_fit_reply()])
        await run_stage(job, backend, Stage.fit_assessment, session, preferences_path=prefs_path)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert "Japanese (ja)" in prompt
        assert "verdict word itself" in prompt
        assert "FIT" in prompt and "UNFIT" in prompt


class TestJobLanguageSnapshotOverridesGlobalPreference:
    """A launched job's `job.language` snapshot (routes_jobs.py::launch_job) wins over
    whatever the global preferences file currently says (see run_stage's language
    resolution in stages.py)."""

    async def test_job_language_wins_over_preferences_file(self, session, tmp_path):
        # Global preference says Japanese, but this job was launched while the
        # global preference was Spanish — its snapshot must be honored instead.
        prefs_path = _write_prefs(tmp_path, "ja")
        job = await _insert_job(session)
        job.language = "es"  # simulates the LAUNCH-time snapshot (routes_jobs.py::launch_job)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session, preferences_path=prefs_path)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert "Spanish (es)" in prompt
        assert "Japanese (ja)" not in prompt

    async def test_no_snapshot_falls_back_to_preferences_file(self, session, tmp_path):
        # job.language is None (never launched through the new endpoint) — falls
        # back to reading the live global preference, as before.
        prefs_path = _write_prefs(tmp_path, "ja")
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session, preferences_path=prefs_path)

        assert "Japanese (ja)" in backend.start_session_prompt


class TestResumedSessionNeverGetsDirective:
    async def test_awaiting_input_resume_has_no_directive(self, session, tmp_path):
        prefs_path = _write_prefs(tmp_path, "fr")
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Pre-seed Message history (so run_stage takes the resume branch) plus an
        # answered FollowUp (so _get_latest_answer finds the user's reply to send).
        from datetime import datetime

        from jsa.db.models import FollowUp, Message

        session.add(Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="hi"))
        session.add(Message(job_id=job.id, stage=Stage.cv_adjust, role="assistant", content="ok"))
        session.add(FollowUp(
            job_id=job.id, stage=Stage.cv_adjust, question="q?", answer="a!",
            answered_at=datetime.utcnow(),
        ))
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session, preferences_path=prefs_path)

        # Resume path uses restore_session, never start_session.
        assert backend.start_session_prompt is None
        assert backend.restore_session_prompt is not None
        assert "## Output language" not in backend.restore_session_prompt


class _CapturingStructuredBackend(FakeAgentBackend):
    """Like _CapturingBackend, but simulates a structured-capable, wire-stateless
    backend (e.g. opencode-go/chat, anthropic) — records the system_prompt passed to
    start_session AND restore_session, plus the structured_schema kwarg."""

    def __init__(self, replies):
        super().__init__(replies, supports_structured_output=True)
        self.start_session_prompt: str | None = None
        self.restore_session_prompt: str | None = None

    async def start_session(self, system_prompt, initial_user_msg, structured_schema=None):
        self.start_session_prompt = system_prompt
        handle, reply = await super().start_session(system_prompt, initial_user_msg, structured_schema)
        return handle, reply

    async def restore_session(self, system_prompt, history, external_id, structured_schema=None):
        self.restore_session_prompt = system_prompt
        return await super().restore_session(system_prompt, history, external_id, structured_schema)


def _structured_final(payload: dict) -> AgentReply:
    raw = json.dumps({"kind": "final", "question": None, "payload": payload})
    return AgentReply(raw=raw, content=json.dumps(payload), kind="final")


class TestStructuredResumeStillGetsContract:
    """Regression test for the stuck-job bug: a resumed session on a structured-
    capable, wire-stateless backend must still carry the structured-output contract
    on every restore_session call — without it, the model silently loses the
    kind/question/payload explanation and can loop forever re-asking its opening
    question (confirmed live: a cv_adjust job on opencode-go/longcat-2.0 re-asked
    "shall I proceed with this strategy" indefinitely after repeated user approval)."""

    async def test_awaiting_input_resume_carries_structured_contract(self, session, tmp_path):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        from datetime import datetime

        from jsa.db.models import FollowUp, Message

        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="hi",
        ))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=json.dumps({"kind": "question", "question": "Shall I proceed?", "payload": None}),
        ))
        session.add(FollowUp(
            job_id=job.id, stage=Stage.cv_adjust, question="Shall I proceed?", answer="Proceed",
            answered_at=datetime.utcnow(),
        ))
        await session.commit()

        backend = _CapturingStructuredBackend([_structured_final(_cv_json())])
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert backend.start_session_prompt is None
        assert backend.restore_session_prompt is not None
        assert "## Structured output contract" in backend.restore_session_prompt
        assert '"kind"' in backend.restore_session_prompt or "`kind`" in backend.restore_session_prompt
        # Still never the language directive on resume (unchanged invariant).
        assert "## Output language" not in backend.restore_session_prompt
