"""Unit tests for jsa.pipeline.stages — run_stage function.

Uses FakeAgentBackend (scripted replies) and in-memory SQLite.
All tests use asyncio_mode = "auto" (configured in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle, ToolCall
from jsa.agents.protocol import ProtocolError
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
from jsa.pipeline.stages import (
    FinalContentError,
    MAX_FINAL_CORRECTIONS,
    PausedForInput,
    _validate_final_content,
    run_stage,
)
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import CapturingBackend, FakeAgentBackend, FakeSessionHandle


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


async def _insert_job(session: AsyncSession, **overrides) -> Job:
    """Insert a job using upsert_job, launch it (queued → pending), and commit."""
    data = _job_data(**overrides)
    job = await repo.upsert_job(session, data)
    job.state = JobState.pending
    await session.commit()
    return job


def _raw_final(content: str) -> AgentReply:
    """A FINAL reply carrying exactly `content` (used for raw JSON / contamination payloads)."""
    return AgentReply(
        raw=f"<<<FINAL>>>\n{content}\n<<<END>>>",
        content=content,
        kind="final",
    )


def _cv_json(marker: str = "Adjusted CV") -> dict:
    """A minimal valid CV object. `marker` is embedded in the summary so callers can
    assert it survives into the serialized Markdown (assertions use substrings)."""
    return {
        "contact": {
            "name": "Jane Doe",
            "email": "jane.doe@example.com",
            "phone": "+1-555-867-5309",
        },
        "sections": [
            {"name": "Summary", "text": f"{marker}: senior engineer with eight years of experience."},
            {"name": "Experience", "entries": [
                {"role": "Senior Engineer", "company": "Acme", "dates": "2019–present",
                 "bullets": ["Built a distributed payment pipeline", "Led a service migration"]},
            ]},
        ],
    }


def _final_reply(marker: str = "Adjusted CV") -> AgentReply:
    """A valid cv_adjust FINAL (CV JSON). `marker` appears in the serialized Markdown."""
    return _raw_final(json.dumps(_cv_json(marker)))


def _tool_loop_miss_reply() -> AgentReply:
    """A prose FINAL reply that the tool-patching loop (jsa/pipeline/tool_loop.py)
    cannot parse as a tool call (``kind`` is ``"final"``, not ``"tool_calls"``).

    Since Phase 5, `run_stage` always attempts `run_tool_loop` first for a
    `revising_cv`/`revising_cl` job whose latest Document already has a `.structured`
    payload (any job that completed a real `run_stage(..., Stage.cv_adjust/
    cover_letter, ...)` call, as opposed to a hand-inserted `Document` row with no
    `structured` field, does). `FakeAgentBackend`'s default `supports_native_tools`
    is False, so the loop enters the PROMPT rung, sends `instruction`, and consumes
    ONE scripted reply attempting to parse it as a `<<<TOOL_CALLS>>>` block; any
    ordinary FINAL reply fails that parse and the loop gives up, returning None so
    `run_stage` falls through to rung 3 (today's unmodified full-rewrite path) —
    which then consumes the NEXT scripted reply. Tests exercising a revision against
    a job with a real prior `structured` Document must therefore prepend one of
    these before the "real" scripted reply."""
    return _raw_final(json.dumps(_cv_json("tool-loop miss — ignored, rung 3 follows")))


def _cv_json_no_summary() -> dict:
    """A schema-valid CV with NO summary section (triggers the soft summary nudge)."""
    return {
        "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
        "sections": [
            {"name": "Experience", "entries": [
                {"role": "Senior Engineer", "company": "Acme", "dates": "2019–present",
                 "bullets": ["Built a distributed payment pipeline"]},
            ]},
            {"name": "Skills", "items": ["Python", "Go"]},
        ],
    }


# A cover letter emitted into the CV shape: valid JSON, contact + one section, but the text
# is letter prose (≥2 letter formulas). The content-kind guard rejects it as not-a-CV.
_COVER_LETTER_AS_CV = json.dumps({
    "contact": {"name": "Jane Doe", "email": "j@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the Gameplay Programmer role.",
        "I would welcome discussing how I can contribute. Sincerely, Jane",
    ]}],
})


def _cl_json(body: str | None = None) -> dict:
    body = body or (
        "I am excited to apply for this role because your mission to build great "
        "developer tooling resonates with my four years of shipping production systems."
    )
    return {"salutation": "Dear Hiring Manager,", "paragraphs": [body], "signoff": "Sincerely,\nAndrei"}


def _cl_final_reply(body: str | None = None) -> AgentReply:
    """A valid cover_letter FINAL (cover-letter JSON)."""
    return _raw_final(json.dumps(_cl_json(body)))


def _needs_input_reply(question: str = "What is your target role?") -> AgentReply:
    return AgentReply(
        raw=f"<<<NEED_INPUT>>>\n{question}\n<<<END>>>",
        content=question,
        kind="needs_input",
        question=question,
    )


# ---------------------------------------------------------------------------
# FakeAgentBackend with traceable external_id (for session_external_id tests)
# ---------------------------------------------------------------------------


class FakeBackendWithExternalId(FakeAgentBackend):
    """FakeAgentBackend that produces handles with a non-None external_id."""

    EXTERNAL_ID = "fake-session-123"

    async def start_session(self, system_prompt, initial_user_msg):
        handle = FakeSessionHandle(id=str(uuid4()), external_id=self.EXTERNAL_ID)
        reply = self._pop_reply()
        return handle, reply


# ---------------------------------------------------------------------------
# Test: Happy path — cv_adjust → cv_done
# ---------------------------------------------------------------------------


class TestCvAdjustHappyPath:
    async def test_job_transitions_to_cv_review(self, session):
        """Fresh cv_adjust session: FINAL reply → job parks in cv_review (the CV gate)."""
        job = await _insert_job(session)
        # Transition to running(cv_adjust) before calling run_stage
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# My Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review
        assert refreshed.current_stage is None

    async def test_document_written_with_cv_adjust_stage(self, session):
        """Document row created with stage=cv_adjust after successful run."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# My Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 1
        assert docs[0].stage == Stage.cv_adjust
        assert docs[0].version == 1
        assert "My Adjusted CV" in docs[0].markdown

    async def test_messages_written_after_cv_adjust(self, session):
        """system, user, assistant Message rows are written after cv_adjust."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session)

        result = await session.execute(
            select(Message).where(Message.job_id == job.id).order_by(Message.id.asc())
        )
        msgs = list(result.scalars().all())
        roles = [m.role for m in msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Test: an unexpected TOOL_CALLS block (revision-tool-use plan, Phase 2) outside a
# tool session hard-fails loudly instead of being misrouted as a FINAL payload.
# run_stage never enters jsa/pipeline/tool_loop.py yet (Phase 5 wiring), so no
# session dispatched here is a tool session — see the guard in run_stage.
# ---------------------------------------------------------------------------


def _tool_calls_reply(name: str = "get_cv") -> AgentReply:
    content = json.dumps([{"name": name, "arguments": {}}])
    return AgentReply(
        raw=f"<<<TOOL_CALLS>>>\n{content}\n<<<END>>>",
        content=content,
        kind="tool_calls",
        tool_calls=[ToolCall(id="call_0", name=name, arguments={})],
    )


class TestUnexpectedToolCallsBlockOutsideToolSession:
    async def test_cv_adjust_hard_fails_with_diagnosable_message(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_tool_calls_reply()])
        with pytest.raises(ProtocolError, match="unexpected tool-call block"):
            await run_stage(job, backend, Stage.cv_adjust, session)

    async def test_does_not_get_the_sentinel_nudge_retry(self, session):
        """A genuine 'no sentinel block' gets one nudge attempt inside the backend
        (claude-cli/opencode-zen's _parse_with_nudge). A TOOL_CALLS block DID parse —
        it's just unexpected here — so it must NOT consume a second scripted reply;
        it fails on the very first (and only) one."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_tool_calls_reply()])
        with pytest.raises(ProtocolError):
            await run_stage(job, backend, Stage.cv_adjust, session)
        assert backend._replies == []  # exactly one reply was consumed, none left over


# ---------------------------------------------------------------------------
# Test: Happy path — cover_letter → cl_done → review
# ---------------------------------------------------------------------------


class TestCoverLetterHappyPath:
    async def test_job_transitions_to_review_after_cover_letter(self, session):
        """cover_letter FINAL reply → job advances to review (not cl_done, which is transient)."""
        job = await _insert_job(session)
        # Simulate that cv_adjust is done; job is now cv_done
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        # Now transition to running(cover_letter)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_cl_final_reply()])
        await run_stage(job, backend, Stage.cover_letter, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage is None

    async def test_document_written_with_cover_letter_stage(self, session):
        """Document row created with stage=cover_letter after successful run."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_cl_final_reply()])
        await run_stage(job, backend, Stage.cover_letter, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cover_letter)
        assert len(docs) == 1
        assert docs[0].stage == Stage.cover_letter
        assert docs[0].version == 1

    async def test_messages_written_after_cover_letter(self, session):
        """system, user, assistant Message rows are written for cover_letter stage."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_cl_final_reply()])
        await run_stage(job, backend, Stage.cover_letter, session)

        result = await session.execute(
            select(Message)
            .where(Message.job_id == job.id, Message.stage == Stage.cover_letter)
            .order_by(Message.id.asc())
        )
        msgs = list(result.scalars().all())
        roles = [m.role for m in msgs]
        assert "system" in roles
        assert "user" in roles
        assert "assistant" in roles


# ---------------------------------------------------------------------------
# Test: Needs input → park to awaiting_input
# ---------------------------------------------------------------------------


class TestNeedsInputParking:
    async def test_paused_for_input_raised(self, session):
        """run_stage raises PausedForInput when agent returns needs_input."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

    async def test_job_transitions_to_awaiting_input(self, session):
        """After PausedForInput, job state is awaiting_input in DB."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.awaiting_input
        assert refreshed.current_stage == Stage.cv_adjust

    async def test_follow_up_row_inserted(self, session):
        """A FollowUp row is written with the question text when parking."""
        question = "What is your target role?"
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_needs_input_reply(question)])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        fus = await repo.get_follow_ups(session, job.id, answered=False)
        assert len(fus) == 1
        assert fus[0].question == question
        assert fus[0].answered_at is None

    async def test_session_external_id_set_on_job(self, session):
        """session_external_id is stored on the job when parking (non-None external_id)."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeBackendWithExternalId([_needs_input_reply("What tier?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.session_external_id == FakeBackendWithExternalId.EXTERNAL_ID


# ---------------------------------------------------------------------------
# Test: Resume after answer
# ---------------------------------------------------------------------------


class TestResumeAfterAnswer:
    async def test_resume_continues_to_cv_review(self, session):
        """Resume after answered FollowUp continues to completion."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: park with needs_input
        backend1 = FakeAgentBackend([_needs_input_reply("What is your target role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        # Simulate user answering: insert FollowUp answer
        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fu = fus[0]
        fu.answer = "Software Engineer"
        fu.answered_at = datetime.utcnow()
        await session.commit()

        # Transition back to running for the resume
        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        # Second run: backend returns FINAL
        backend2 = FakeAgentBackend([_final_reply("# Adjusted CV after answer")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review

    async def test_resume_writes_only_new_messages(self, session):
        """On resume, only the new answer + reply messages are added (not re-written)."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: park
        backend1 = FakeAgentBackend([_needs_input_reply("Role?")])
        with pytest.raises(PausedForInput):
            await run_stage(job, backend1, Stage.cv_adjust, session)

        msgs_after_park_result = await session.execute(
            select(Message).where(Message.job_id == job.id)
        )
        msgs_after_park = list(msgs_after_park_result.scalars().all())
        count_before_resume = len(msgs_after_park)

        # Answer and resume
        fus = await repo.get_follow_ups(session, job.id, answered=False)
        fus[0].answer = "SWE"
        fus[0].answered_at = datetime.utcnow()
        await session.commit()

        job_fresh = await repo.get_job(session, job.id)
        transition(job_fresh, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend2 = FakeAgentBackend([_final_reply("# Final CV")])
        await run_stage(job_fresh, backend2, Stage.cv_adjust, session)

        msgs_after_resume_result = await session.execute(
            select(Message).where(Message.job_id == job.id)
        )
        msgs_after_resume = list(msgs_after_resume_result.scalars().all())
        # Two new messages: user answer and assistant final reply
        assert len(msgs_after_resume) == count_before_resume + 2


# ---------------------------------------------------------------------------
# Test: Revision flow — revising_cv
# ---------------------------------------------------------------------------


class TestRevisionFlow:
    async def _setup_job_in_review(self, session: AsyncSession) -> Job:
        """Helper: create a job in review state with a cv_adjust Document."""
        job = await _insert_job(session)
        # Set up state as if cv_adjust and cover_letter completed
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        transition(job, JobState.cl_done, None)
        transition(job, JobState.review, None)
        await session.commit()

        # Insert initial CV document and some message history for cv_adjust
        doc = Document(
            job_id=job.id,
            stage=Stage.cv_adjust,
            version=1,
            markdown="# Original CV",
        )
        session.add(doc)
        # Add prior messages for cv_adjust (needed by restore_session history load)
        msg_user = Message(
            job_id=job.id,
            stage=Stage.cv_adjust,
            role="user",
            content="Initial CV text",
        )
        msg_asst = Message(
            job_id=job.id,
            stage=Stage.cv_adjust,
            role="assistant",
            content="# Original CV",
        )
        session.add(msg_user)
        session.add(msg_asst)
        # Set per-stage session IDs so the BF-9 null-session guard does not fire.
        job.cv_session_id = "fake-session-cv-123"
        job.cl_session_id = "fake-session-cl-123"
        await session.commit()
        return job

    async def test_revision_produces_new_document_version(self, session):
        """revising_cv: FINAL → new Document version written."""
        job = await self._setup_job_in_review(session)

        # Insert RevisionRequest
        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        # Transition to running(revising_cv)
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 2  # original + revised
        versions = sorted(d.version for d in docs)
        assert versions == [1, 2]

    async def test_revision_returns_to_review_state(self, session):
        """revising_cv: after FINAL, job returns to review state."""
        job = await self._setup_job_in_review(session)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review
        assert refreshed.current_stage is None

    async def test_revision_consumes_revision_request(self, session):
        """revising_cv: RevisionRequest.consumed_at is set after completion."""
        job = await self._setup_job_in_review(session)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Make it shorter",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_final_reply("# Shorter CV")])
        await run_stage(job, backend, Stage.revising_cv, session)

        result = await session.execute(
            select(RevisionRequest).where(RevisionRequest.job_id == job.id)
        )
        rr_refreshed = result.scalar_one()
        assert rr_refreshed.consumed_at is not None


# ---------------------------------------------------------------------------
# Test: Document versioning
# ---------------------------------------------------------------------------


class TestDocumentVersioning:
    async def test_two_revisions_produce_version_1_and_2(self, session):
        """Running cv_adjust twice (via revision) yields versions 1 and 2."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # First run: fresh cv_adjust → version 1
        backend1 = FakeAgentBackend([_final_reply("# CV v1")])
        await run_stage(job, backend1, Stage.cv_adjust, session)

        docs_v1 = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs_v1) == 1
        assert docs_v1[0].version == 1

        # Now simulate revision: set up review state and insert RevisionRequest
        # Need to go through the CV gate (approve) then cover_letter to reach review state
        job_after_cv = await repo.get_job(session, job.id)
        transition(job_after_cv, JobState.cv_done, None)  # simulate approve-cv
        transition(job_after_cv, JobState.running, Stage.cover_letter)
        transition(job_after_cv, JobState.cl_done, None)
        transition(job_after_cv, JobState.review, None)
        await session.commit()

        # Add message history for cv_adjust (required for restore_session in revision)
        msg_user = Message(
            job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial"
        )
        msg_asst = Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant", content="# CV v1"
        )
        session.add(msg_user)
        session.add(msg_asst)

        rr = RevisionRequest(
            job_id=job.id,
            target=Stage.cv_adjust,
            instruction="Expand the skills section",
            consumed_at=None,
        )
        session.add(rr)
        await session.commit()

        job_for_revision = await repo.get_job(session, job.id)
        # Set per-stage session IDs so the BF-9 null-session guard does not fire.
        job_for_revision.cv_session_id = "fake-session-cv-123"
        job_for_revision.cl_session_id = "fake-session-cl-123"
        transition(job_for_revision, JobState.running, Stage.revising_cv)
        await session.commit()

        # See _tool_loop_miss_reply's docstring: the first cv_adjust run above went
        # through _handle_final for real, so this job's cv_adjust Document has a
        # `.structured` payload — the tool loop is genuinely attempted here.
        backend2 = FakeAgentBackend([_tool_loop_miss_reply(), _final_reply("# CV v2 (expanded)")])
        await run_stage(job_for_revision, backend2, Stage.revising_cv, session)

        docs_v2 = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs_v2) == 2
        versions = sorted(d.version for d in docs_v2)
        assert versions == [1, 2]
        latest = max(docs_v2, key=lambda d: d.version)
        assert "CV v2" in latest.markdown


# ---------------------------------------------------------------------------
# _validate_cl_content unit tests
# ---------------------------------------------------------------------------

_REAL_LETTER = """\
Dear Hiring Manager,

I am writing to express my strong interest in the Developer Relations role at Rive. \
Having spent the last four years building real-time graphics tooling with Unreal Engine's \
Blueprint and native C++ layers, I am drawn to Rive's mission of making interactive \
animation accessible to every developer on every platform.

My work on Promise/Future async wrappers and UStructSynchronizer gave me a deep \
appreciation for the gap between a powerful runtime and the tooling that lets \
developers trust it. I would bring the same instinct to Rive's SDK and documentation \
surface, turning edge-case discoveries into clear, reproducible examples.

I would welcome the chance to discuss how my background aligns with what your team \
is building. Thank you for your time.

Sincerely,
Andrei Kharlanchev
"""


class TestValidateClContent:
    """Cover-letter FINAL is now validated as JSON against the CoverLetter schema."""

    def test_valid_cover_letter_json_passes(self):
        obj = _validate_final_content(Stage.cover_letter, json.dumps(_cl_json()), None)
        assert obj.__class__.__name__ == "CoverLetter"

    def test_fenced_json_tolerated(self):
        payload = "```json\n" + json.dumps(_cl_json()) + "\n```"
        _validate_final_content(Stage.revising_cl, payload, None)  # must not raise

    def test_prose_letter_rejected(self):
        # The model emitting the letter as plain prose (not JSON) now fails to parse.
        with pytest.raises(FinalContentError):
            _validate_final_content(Stage.cover_letter, _REAL_LETTER, None)

    def test_third_person_summary_rejected(self):
        # A description of the letter is not valid cover-letter JSON.
        bad = "The letter emphasizes the candidate's strengths and aligns them with the role."
        with pytest.raises(FinalContentError):
            _validate_final_content(Stage.cover_letter, bad, None)

    def test_too_short_body_rejected(self):
        payload = json.dumps({"paragraphs": ["Hi."]})
        with pytest.raises(FinalContentError, match="schema"):
            _validate_final_content(Stage.cover_letter, payload, None)

    def test_unknown_key_ignored_not_fatal(self):
        # The schema is tolerant (extra="ignore"): a leaked "centerpiece"/meta field does
        # not fail validation. Contamination defense lives in the serializer, which renders
        # only known fields — so the stray key never reaches the output.
        from jsa.render.serialize import cover_letter_to_markdown
        payload = json.dumps({"paragraphs": ["x" * 200], "centerpiece": "meta-commentary"})
        obj = _validate_final_content(Stage.cover_letter, payload, None)
        md = cover_letter_to_markdown(obj)
        assert "meta-commentary" not in md and "centerpiece" not in md


# ---------------------------------------------------------------------------
# _validate_cv_content unit tests (positive validation against the base CV)
# ---------------------------------------------------------------------------

_REAL_CV = """\
# Jane Doe
jane.doe@example.com | +1-555-867-5309 | linkedin.com/in/janedoe | Austin, USA

---
## Summary
Senior software engineer with eight years building reliable backend services and
developer tooling. Proven record of leading teams and shipping production systems.

---
## Experience
**Acme Corp** — Senior Engineer (2019–present)
- Built a distributed payment pipeline handling 2M requests/day
- Led migration to a typed service architecture, cutting incident rate by 40%

---
## Skills
Python, Go, PostgreSQL, Kubernetes, CI/CD (Continuous Integration/Continuous Delivery)
"""

# The real-world failure that motivated this guard: a "task complete" summary the
# agent emitted *inside* <<<FINAL>>> instead of the CV (carries no contact anchors).
_SUMMARY_NOT_A_CV = (
    "**CV Finalized and Ready**\n\n"
    "Your revised CV has been prepared with all reframing integrated:\n\n"
    "✅ Reframed 4 bullets to emphasize simulation environment design and telemetry.\n"
    "✅ Skills, projects, portfolios — unchanged.\n"
    "✅ ATS keywords added naturally: scenario, telemetry, physics-based, simulation.\n\n"
    "The file is ready to write to /Users/me/cv-final.md. Next steps: once you approve "
    "the write, you can proceed to cover letter drafting, or submit the CV as-is."
)


class TestValidateCvContent:
    """cv_adjust FINAL is now validated as JSON against the CVDocument schema."""

    def test_valid_cv_json_passes(self):
        obj = _validate_final_content(Stage.cv_adjust, json.dumps(_cv_json()), None)
        assert obj.__class__.__name__ == "CVDocument"

    def test_markdown_cv_rejected(self):
        # The base CV is Markdown; emitting Markdown (not JSON) into FINAL now fails.
        with pytest.raises(FinalContentError):
            _validate_final_content(Stage.cv_adjust, _REAL_CV, None)

    def test_real_world_summary_rejected(self):
        # Reproduces the production bug — a "task complete" summary instead of the CV.
        with pytest.raises(FinalContentError):
            _validate_final_content(Stage.cv_adjust, _SUMMARY_NOT_A_CV, None)

    def test_contact_name_only_accepted(self):
        # Tolerant by design: a contact with only a name (no email/phone) is accepted.
        # Rejecting it would false-fail a whole job over missing contact formatting; the
        # CV still renders and the user reviews it. `name` + >=1 section is the only gate.
        payload = json.dumps({"contact": {"name": "Jane"},
                              "sections": [{"name": "Summary", "text": "hi there friend"}]})
        obj = _validate_final_content(Stage.cv_adjust, payload, None)
        assert obj.contact.name == "Jane"

    def test_missing_contact_name_rejected(self):
        # The one contact anchor that distinguishes a CV from noise is still required.
        bad = json.dumps({"contact": {"email": "j@x.com"},
                          "sections": [{"name": "Summary", "text": "hi there friend"}]})
        with pytest.raises(FinalContentError, match="schema"):
            _validate_final_content(Stage.cv_adjust, bad, None)

    def test_empty_sections_rejected(self):
        bad = json.dumps({"contact": {"name": "Jane", "email": "j@x.com"}, "sections": []})
        with pytest.raises(FinalContentError):
            _validate_final_content(Stage.cv_adjust, bad, None)

    def test_unknown_section_key_ignored(self):
        # Tolerant: a leaked change-log field on a section is ignored, not fatal, and the
        # serializer (rendering only known fields) keeps it out of the output.
        from jsa.render.serialize import cv_to_markdown
        payload = json.dumps({"contact": {"name": "Jane", "email": "j@x.com"},
                              "sections": [{"name": "Summary", "text": "hello there", "changelog": "x"}]})
        obj = _validate_final_content(Stage.cv_adjust, payload, None)
        md = cv_to_markdown(obj)
        assert "hello there" in md and "changelog" not in md

    def test_skeleton_cv_rejected(self):
        # The live bug (2026-09-11, gemini-3.5-flash): structured output came back
        # schema-valid as a Summary plus three sections carrying `text: null`, passed the
        # old "at least one renderable section" gate, and was rendered to PDF/DOCX as a
        # one-paragraph "CV". Verbatim shape of jobs a1145262/37c04e8d/f7066f23/… in
        # ~/.jsa/jsa.sqlite. The prompt-side half of this fix is
        # `prompt_assembly._completeness_clause`; this is the structural gate.
        bad = json.dumps({
            "contact": {"name": "Andrei", "email": "a@b.c"},
            "sections": [
                {"name": "Summary", "text": "Senior UE5 C++ developer with 4 years."},
                {"name": "Skills", "text": None},
                {"name": "Experience", "text": None},
                {"name": "Technical Projects", "text": None},
            ],
        })
        with pytest.raises(FinalContentError, match="heading-only outline"):
            _validate_final_content(Stage.cv_adjust, bad, None)

    def test_one_stray_empty_section_still_tolerated(self):
        # The long-standing tolerance the skeleton arm must NOT break: a single empty/odd
        # section is skipped by the serializer and can't false-reject an otherwise-good CV.
        payload = json.dumps({
            "contact": {"name": "Jane", "email": "j@x.com"},
            "sections": [
                {"name": "Summary", "text": "Engineer with ten years of experience."},
                {"name": "Skills", "items": ["Python", "Go"]},
                {"name": "Experience", "entries": [{"heading": "Dev", "bullets": ["shipped"]}]},
                {"name": "Education", "text": None},
            ],
        })
        obj = _validate_final_content(Stage.cv_adjust, payload, None)
        assert len(obj.sections) == 4

    def test_cover_letter_as_cv_rejected(self):
        # Content-kind guard (the live bug): the model emitted cover-letter prose into the CV
        # shape — valid JSON, contact + a section, so it passed the tolerant gate and shipped
        # a letter labelled "CV". Two+ letter formulas now reject it as the wrong content-kind.
        with pytest.raises(FinalContentError, match="cover letter"):
            _validate_final_content(Stage.cv_adjust, _COVER_LETTER_AS_CV, None)

    def test_single_letter_phrase_tolerated(self):
        # One incidental letter-ish phrase is below the 2-match threshold → accepted. A real
        # CV summary may legitimately contain a single such phrase; we don't false-reject it.
        payload = json.dumps({
            "contact": {"name": "Jane", "email": "j@x.com"},
            "sections": [
                {"name": "Summary", "text": "Engineer who I look forward to hearing feedback."},
                {"name": "Skills", "items": ["Python", "Go"]},
            ],
        })
        obj = _validate_final_content(Stage.cv_adjust, payload, None)
        assert obj.contact.name == "Jane"


# ---------------------------------------------------------------------------
# Self-heal: a FINAL that fails validation is re-prompted on the same session
# ---------------------------------------------------------------------------


class TestCvAdjustSelfHeal:
    async def test_recovers_after_bad_final(self, session):
        """A summary FINAL is re-prompted; the corrected CV JSON is accepted → cv_done."""
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_raw_final(_SUMMARY_NOT_A_CV), _final_reply("Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review

        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 1
        assert "Jane Doe" in docs[0].markdown  # serialized from the recovered CV JSON
        assert "CV Finalized" not in docs[0].markdown  # the summary was rejected
        assert docs[0].structured is not None  # JSON source-of-truth persisted

        # The corrective turn (user + assistant) was recorded in history.
        result = await session.execute(
            select(Message).where(Message.job_id == job.id, Message.role == "user")
        )
        user_msgs = [m.content for m in result.scalars().all()]
        assert any("not a valid CV JSON object" in m for m in user_msgs)

    async def test_fails_after_exhausting_corrections(self, session):
        """Persistent bad FINALs exhaust corrections → FinalContentError, job not cv_done."""
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        # 1 initial + MAX_FINAL_CORRECTIONS re-prompts, all bad.
        replies = [_raw_final(_SUMMARY_NOT_A_CV) for _ in range(1 + MAX_FINAL_CORRECTIONS)]
        backend = FakeAgentBackend(replies)

        with pytest.raises(FinalContentError):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state != JobState.cv_review
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert docs == []  # nothing was persisted


class TestCvAdjustSummaryNudge:
    """A valid CV with no summary is *nudged* (soft) — never hard-failed."""

    async def test_missing_summary_nudged_then_accepted(self, session):
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([
            _raw_final(json.dumps(_cv_json_no_summary())),  # valid but no Summary
            _final_reply("Adjusted CV"),                    # corrected: has a Summary
        ])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert "## Summary" in docs[0].markdown

        result = await session.execute(
            select(Message).where(Message.job_id == job.id, Message.role == "user")
        )
        assert any("missing a Summary" in m.content for m in result.scalars().all())

    async def test_persistent_missing_summary_ships_thin_not_failed(self, session):
        """A CV that never gains a summary still ships (cv_review): thinness, not corruption."""
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        replies = [_raw_final(json.dumps(_cv_json_no_summary()))
                   for _ in range(1 + MAX_FINAL_CORRECTIONS)]
        backend = FakeAgentBackend(replies)
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review  # soft: shipped, not failed
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert len(docs) == 1
        assert "## Summary" not in docs[0].markdown


class TestCvAdjustCoverLetterGuard:
    """A cover letter emitted into the CV shape is corruption — hard-gated → fail if uncorrected."""

    async def test_cover_letter_as_cv_recovers(self, session):
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = FakeAgentBackend([_raw_final(_COVER_LETTER_AS_CV), _final_reply("Adjusted CV")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review
        # the concise validator reason ("...cover letter...") was forwarded to the model
        result = await session.execute(
            select(Message).where(Message.job_id == job.id, Message.role == "user")
        )
        assert any("cover letter" in m.content for m in result.scalars().all())

    async def test_persistent_cover_letter_fails(self, session):
        job = await _insert_job(session, cv_text=_REAL_CV)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        replies = [_raw_final(_COVER_LETTER_AS_CV) for _ in range(1 + MAX_FINAL_CORRECTIONS)]
        backend = FakeAgentBackend(replies)
        with pytest.raises(FinalContentError):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state != JobState.cv_review
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert docs == []


# ---------------------------------------------------------------------------
# Phase 2B: cv_adjust consumes the standalone base-CV structure as a skeleton
# ---------------------------------------------------------------------------


_CapturingBackend = CapturingBackend


class TestCvAdjustConsumesBaseStructure:
    async def test_skeleton_injected_when_file_present(self, session, tmp_path):
        # Write a base structure with a distinctively-named section.
        structure_path = tmp_path / "cv_structure.json"
        structure_path.write_text(json.dumps({
            "contact": {"name": "Jane Doe", "email": "jane@x.com"},
            "sections": [
                {"name": "Summary", "text": "Backend engineer."},
                {"name": "Open Source Leadership", "items": ["Maintainer of Foo"]},
            ],
        }), encoding="utf-8")

        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(
            job, backend, Stage.cv_adjust, session, cv_structure_path=structure_path
        )

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "BASE CV STRUCTURE" in msg
        assert "Open Source Leadership" in msg  # the curated section name was injected
        assert "CV TEXT:" not in msg  # structure JSON is the only CV content now

    async def test_no_skeleton_and_no_raw_cv_when_file_absent(self, session, tmp_path):
        # Path points at a non-existent file → no skeleton AND no raw cv_text fallback;
        # cv_text is deprecated and never injected into any prompt.
        missing = tmp_path / "nope.json"

        job = await _insert_job(session, cv_text="MY UNIQUE CV BODY")
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session, cv_structure_path=missing)

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "BASE CV STRUCTURE" not in msg
        assert "MY UNIQUE CV BODY" not in msg
        assert "CV TEXT:" not in msg

    async def test_skeleton_falls_back_for_cover_letter_when_no_tailored_cv(self, session, tmp_path):
        # cover_letter's primary CV content is the approved tailored CV Document (see
        # TestCoverLetterUsesTailoredCv below) — but when none exists yet (should be
        # unreachable past the CV gate; defensive only), it falls back to the base
        # structure so the agent isn't left with zero CV content. See CVL_PROMPT.md step 2.
        structure_path = tmp_path / "cv_structure.json"
        structure_path.write_text(json.dumps({
            "contact": {"name": "Jane Doe"},
            "sections": [{"name": "Open Source Leadership", "items": ["Maintainer"]}],
        }), encoding="utf-8")

        job = await _insert_job(session)
        # cover_letter runs from cv_done; reach it via running(cv_adjust) like the suite does.
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = _CapturingBackend([_cl_final_reply()])
        await run_stage(
            job, backend, Stage.cover_letter, session, cv_structure_path=structure_path
        )

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "BASE CV STRUCTURE" in msg
        assert "Open Source Leadership" in msg


# ---------------------------------------------------------------------------
# Two-lane pipeline: cover_letter is written against the approved tailored CV,
# not the base structure; cv_adjust no longer runs research.
# ---------------------------------------------------------------------------


class TestCoverLetterUsesTailoredCv:
    async def test_cover_letter_initial_msg_contains_tailored_cv_markdown(self, session, tmp_path):
        """When a cv_adjust Document exists, cover_letter's initial message carries its
        markdown under a TAILORED CV label — not the base structure."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        doc = Document(
            job_id=job.id,
            stage=Stage.cv_adjust,
            version=1,
            markdown="# Jane Doe\n\nA distinctively-worded tailored CV body.",
        )
        session.add(doc)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = _CapturingBackend([_cl_final_reply()])
        await run_stage(job, backend, Stage.cover_letter, session)

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "TAILORED CV" in msg
        assert "A distinctively-worded tailored CV body." in msg
        assert "BASE CV STRUCTURE" not in msg

    async def test_cover_letter_falls_back_to_base_structure_without_tailored_cv(
        self, session, tmp_path
    ):
        """No cv_adjust Document yet (defensive; should be unreachable past the CV gate)
        → falls back to the base structure rather than sending no CV content at all."""
        structure_path = tmp_path / "cv_structure.json"
        structure_path.write_text(json.dumps({
            "contact": {"name": "Jane Doe"},
            "sections": [{"name": "Summary", "text": "Fallback base CV."}],
        }), encoding="utf-8")

        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = _CapturingBackend([_cl_final_reply()])
        await run_stage(
            job, backend, Stage.cover_letter, session, cv_structure_path=structure_path
        )

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "BASE CV STRUCTURE" in msg
        assert "TAILORED CV" not in msg


class TestCvAdjustResearchUnwired:
    """cv_adjust no longer gets a research brief block — research is cover_letter-only."""

    async def test_cv_adjust_initial_msg_has_no_brief_block(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _CapturingBackend([_final_reply()])
        await run_stage(job, backend, Stage.cv_adjust, session)

        msg = backend.captured_initial_msg
        assert msg is not None
        assert "[INTEL_BRIEF]" not in msg
        assert "[COMPANY_BRIEF]" not in msg
        assert msg.startswith("JOB DESCRIPTION:")

    def test_research_spec_raises_for_cv_adjust(self):
        from jsa.pipeline.stages import _research_spec

        job = Job(
            id="aabbccdd00112233", company="Acme", role="Engineer",
            link="https://acme.com/job", tier="A", jd="JD", jd_hash="hash0000deadbeef",
            cv_text="", state=JobState.pending,
        )
        with pytest.raises(ValueError):
            _research_spec(job, Stage.cv_adjust)
