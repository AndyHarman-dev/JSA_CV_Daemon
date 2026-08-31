"""Phase 5 of the structured-output plan: pipeline wiring, mode-aware self-heal, and
the structured-mode ProtocolError budget (jsa/pipeline/stages.py).

Layers:
1. Unit tests for the new helpers: `_structured_schema_for`, `_log_session_mode`,
   the mode-aware `_parse_structured` wording (with a sentinel-mode byte-identity
   pin), `_start_session_with_retry`, `_send_message_with_wire_retry`.
2. Integration tests through `run_stage`: fresh + resumed sessions on a
   structured-capable fake backend, the fit-capability trap (fit backend's OWN
   capability decides its mode, independent of the general-purpose backend), the
   newly-unlocked sentinel→structured replay direction (previously impossible when
   `adapt_history`'s flag was hardcoded `False`), the fresh-session retry-then-succeed
   and budget-exhausted paths end-to-end, and the structured-worded self-heal
   correction text reaching the model.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, SessionHandle
from jsa.agents.protocol import ProtocolError
from jsa.db import repo
from jsa.db.models import Base, FollowUp, Job, JobState, Message, Stage
from jsa.pipeline.stages import (
    MAX_FINAL_CORRECTIONS,
    FinalContentError,
    _log_session_mode,
    _parse_structured,
    _send_message_with_wire_retry,
    _start_session_with_retry,
    _structured_schema_for,
    run_stage,
)
from jsa.pipeline.state_machine import transition
from jsa.schema.cv import CVDocument
from jsa.schema.turn_models import json_schema_for
from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle
from tests.backend.fakes.finals import cv_final


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
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


async def _insert_job(session: AsyncSession, job_id: str = "phase5job0000001") -> Job:
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
    job = await repo.upsert_job(session, data)
    job.state = JobState.pending
    await session.commit()
    return job


_CV_PAYLOAD = {
    "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
    "sections": [{"name": "Summary", "text": "Structured-mode CV: senior engineer."}],
}


def _canonical_final(payload: dict) -> AgentReply:
    raw = json.dumps({"kind": "final", "question": None, "payload": payload})
    return AgentReply(raw=raw, content=json.dumps(payload), kind="final")


def _canonical_final_str(payload: dict) -> str:
    return json.dumps({"kind": "final", "question": None, "payload": payload})


# ---------------------------------------------------------------------------
# _structured_schema_for
# ---------------------------------------------------------------------------


class TestStructuredSchemaFor:
    def test_none_for_non_structured_backend(self):
        backend = FakeAgentBackend([])
        assert _structured_schema_for(backend, Stage.cv_adjust) is None

    def test_schema_for_structured_backend_matches_stage(self):
        backend = FakeAgentBackend([], supports_structured_output=True)
        assert _structured_schema_for(backend, Stage.cv_adjust) == json_schema_for(Stage.cv_adjust)
        assert _structured_schema_for(backend, Stage.cover_letter) == json_schema_for(Stage.cover_letter)
        assert _structured_schema_for(backend, Stage.fit_assessment) == json_schema_for(Stage.fit_assessment)


# ---------------------------------------------------------------------------
# _log_session_mode
# ---------------------------------------------------------------------------


def _log_texts(mock_pub: AsyncMock) -> list[str]:
    return [c.args[0].get("text", "") for c in mock_pub.call_args_list if c.args[0].get("type") == "log"]


class TestLogSessionMode:
    async def test_sentinel_when_schema_none(self, session):
        job = await _insert_job(session)
        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await _log_session_mode(job, Stage.cv_adjust, None, FakeSessionHandle(id="x"))
        assert any("session mode = sentinel" in t for t in _log_texts(mock_pub))

    async def test_structured_when_schema_present_and_not_downgraded(self, session):
        job = await _insert_job(session)
        handle = FakeSessionHandle(id="x", structured_enabled=True)
        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await _log_session_mode(job, Stage.cv_adjust, {"type": "object"}, handle)
        assert any("session mode = structured" in t for t in _log_texts(mock_pub))

    async def test_downgraded_when_handle_reports_disabled(self, session):
        job = await _insert_job(session)
        handle = FakeSessionHandle(id="x", structured_enabled=False)
        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await _log_session_mode(job, Stage.cv_adjust, {"type": "object"}, handle)
        assert any("session mode = sentinel (downgraded)" in t for t in _log_texts(mock_pub))

    async def test_defaults_to_not_downgraded_when_handle_lacks_the_attribute(self, session):
        """AnthropicSessionHandle (and any bare SessionHandle) has no
        `structured_enabled` — must default to "not downgraded", never mislabel a
        never-downgrading backend as downgraded (advisor-flagged sharp edge)."""
        job = await _insert_job(session)
        handle = SessionHandle(id="x")
        mock_pub = AsyncMock()
        with patch("jsa.events.bus.bus.publish", mock_pub):
            await _log_session_mode(job, Stage.cv_adjust, {"type": "object"}, handle)
        assert any("session mode = structured" in t for t in _log_texts(mock_pub))


# ---------------------------------------------------------------------------
# _parse_structured — mode-aware wording, sentinel-mode byte-identity
# ---------------------------------------------------------------------------


class TestParseStructuredModeAwareWording:
    def test_sentinel_mode_json_error_is_byte_identical_to_pre_phase5(self):
        with pytest.raises(FinalContentError) as exc:
            _parse_structured("not json", CVDocument, "cv_adjust", structured=False)
        assert str(exc.value) == (
            "cv_adjust FINAL block was not valid JSON (Expecting value: line 1 column 1 "
            "(char 0)). Re-emit ONLY a single JSON object conforming to the schema inside "
            "<<<FINAL>>>...<<<END>>>."
        )

    def test_structured_mode_json_error_has_no_sentinel_mention(self):
        with pytest.raises(FinalContentError) as exc:
            _parse_structured("not json", CVDocument, "cv_adjust", structured=True)
        msg = str(exc.value)
        assert "<<<" not in msg
        assert "payload" in msg

    def test_sentinel_mode_schema_error_is_byte_identical_to_pre_phase5(self):
        bad = json.dumps({"contact": {"email": "j@x.com"}, "sections": []})
        with pytest.raises(FinalContentError) as exc:
            _parse_structured(bad, CVDocument, "cv_adjust", structured=False)
        assert str(exc.value).endswith(
            "Re-emit a corrected JSON object inside <<<FINAL>>>...<<<END>>>."
        )

    def test_structured_mode_schema_error_has_no_sentinel_mention(self):
        bad = json.dumps({"contact": {"email": "j@x.com"}, "sections": []})
        with pytest.raises(FinalContentError) as exc:
            _parse_structured(bad, CVDocument, "cv_adjust", structured=True)
        msg = str(exc.value)
        assert "<<<" not in msg
        assert msg.endswith("Re-emit a corrected JSON object as your structured reply's `payload`.")


# ---------------------------------------------------------------------------
# _start_session_with_retry / _send_message_with_wire_retry
# ---------------------------------------------------------------------------


class _FlakyStartBackend(FakeAgentBackend):
    """Raises ProtocolError from start_session `fail_times` times, then succeeds."""

    def __init__(self, replies, fail_times: int):
        super().__init__(replies, supports_structured_output=True)
        self._fail_times = fail_times
        self.call_count = 0

    async def start_session(self, system_prompt, initial_user_msg, structured_schema=None):
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise ProtocolError("structured reply unparseable: bad json")
        return await super().start_session(system_prompt, initial_user_msg, structured_schema)


class _FlakySendBackend(FakeAgentBackend):
    """Raises ProtocolError from send_message `fail_times` times, then succeeds."""

    def __init__(self, replies, fail_times: int):
        super().__init__(replies, supports_structured_output=True)
        self._fail_times = fail_times
        self.call_count = 0
        self.sent_texts: list[str] = []

    async def send_message(self, handle, text, structured_schema=None):
        self.call_count += 1
        self.sent_texts.append(text)
        if self.call_count <= self._fail_times:
            raise ProtocolError("structured reply unparseable: bad json")
        return self._pop_reply()


class TestStartSessionWithRetry:
    async def test_sentinel_mode_never_retries(self):
        backend = _FlakyStartBackend([], fail_times=99)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        with pytest.raises(ProtocolError):
            await _start_session_with_retry(backend, "sys", "msg", None, Stage.cv_adjust, job)
        assert backend.call_count == 1  # no retry when schema is None

    async def test_structured_mode_retries_then_succeeds(self):
        backend = _FlakyStartBackend([_canonical_final(_CV_PAYLOAD)], fail_times=MAX_FINAL_CORRECTIONS)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        schema = json_schema_for(Stage.cv_adjust)
        handle, reply = await _start_session_with_retry(backend, "sys", "msg", schema, Stage.cv_adjust, job)
        assert reply.kind == "final"
        assert backend.call_count == MAX_FINAL_CORRECTIONS + 1

    async def test_structured_mode_budget_exhausted_reraises(self):
        backend = _FlakyStartBackend([], fail_times=99)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        schema = json_schema_for(Stage.cv_adjust)
        with pytest.raises(ProtocolError):
            await _start_session_with_retry(backend, "sys", "msg", schema, Stage.cv_adjust, job)
        assert backend.call_count == MAX_FINAL_CORRECTIONS + 1


class TestSendMessageWithWireRetry:
    async def test_sentinel_mode_never_retries(self):
        backend = _FlakySendBackend([], fail_times=99)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        handle = FakeSessionHandle(id="h1")
        with pytest.raises(ProtocolError):
            await _send_message_with_wire_retry(backend, handle, "hello", None, Stage.cv_adjust, job)
        assert backend.call_count == 1

    async def test_structured_mode_recovers_with_correction_and_returns_one_pair(self):
        backend = _FlakySendBackend([_canonical_final(_CV_PAYLOAD)], fail_times=1)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        handle = FakeSessionHandle(id="h1")
        schema = json_schema_for(Stage.cv_adjust)
        reply, msgs = await _send_message_with_wire_retry(
            backend, handle, "original text", schema, Stage.cv_adjust, job
        )
        assert reply.kind == "final"
        # Exactly one user/assistant pair — for the correction that succeeded, not the
        # failed original attempt (which the backend never persisted either).
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert "original text" in msgs[0]["content"]
        assert msgs[0]["content"] != "original text"  # it's the wrapped correction
        assert msgs[1] == {"role": "assistant", "content": reply.raw}

    async def test_structured_mode_budget_exhausted_reraises(self):
        backend = _FlakySendBackend([], fail_times=99)
        job = Job(id="j1", company="A", role="B", link="l", tier="A", jd="jd", jd_hash="h")
        handle = FakeSessionHandle(id="h1")
        schema = json_schema_for(Stage.cv_adjust)
        with pytest.raises(ProtocolError):
            await _send_message_with_wire_retry(backend, handle, "hello", schema, Stage.cv_adjust, job)
        assert backend.call_count == MAX_FINAL_CORRECTIONS + 1


# ---------------------------------------------------------------------------
# Integration: run_stage on a structured-capable fake backend
# ---------------------------------------------------------------------------


class _SystemPromptCapturingBackend(FakeAgentBackend):
    """Records the system_prompt passed to start_session, mirroring
    test_language_directive.py's _CapturingBackend but structured-mode-aware."""

    def __init__(self, replies, **kwargs):
        super().__init__(replies, **kwargs)
        self.start_session_prompt: str | None = None

    async def start_session(self, system_prompt, initial_user_msg, structured_schema=None):
        self.start_session_prompt = system_prompt
        return await super().start_session(system_prompt, initial_user_msg, structured_schema)


class TestFreshSessionStructuredWiring:
    async def test_cv_adjust_fresh_session_receives_schema_and_contract(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _SystemPromptCapturingBackend(
            [_canonical_final(_CV_PAYLOAD)], supports_structured_output=True
        )
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert backend.received_schemas == [json_schema_for(Stage.cv_adjust)]
        prompt = backend.start_session_prompt
        assert prompt is not None
        assert "Structured output contract" in prompt
        assert "supersedes" in prompt
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review
        docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
        assert "Jane Doe" in docs[0].markdown
        assert docs[0].structured is not None

    async def test_sentinel_mode_fresh_session_has_no_structured_contract(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _SystemPromptCapturingBackend([cv_final("Sentinel")])
        await run_stage(job, backend, Stage.cv_adjust, session)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert "Structured output contract" not in prompt


class TestFitCapabilityTrap:
    async def test_fit_backend_own_capability_decides_its_mode_not_general_purpose(self, session):
        """A fit_model override (e.g. google-cli) must decide fit's structured mode from
        ITS OWN supports_structured_output — never from the pipeline's general_purpose
        backend, even when that one is structured-capable (the fit-capability trap the
        plan's Phase 5 Problems/Bugs section names)."""
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        fit_backend = FakeAgentBackend(
            [AgentReply(raw="<<<FINAL>>>\nFIT\ngood match\n<<<END>>>", content="FIT\ngood match", kind="final")],
            supports_structured_output=False,
        )
        general_purpose_backend = FakeAgentBackend([], supports_structured_output=True)

        await run_stage(
            job, general_purpose_backend, Stage.fit_assessment, session, fit_backend=fit_backend
        )

        assert fit_backend.received_schemas == [None]
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.fit_done

    async def test_fit_backend_structured_when_it_is_the_capable_one(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.fit_assessment)
        await session.commit()

        fit_backend = FakeAgentBackend(
            [AgentReply(
                raw=json.dumps({"verdict": "FIT", "reason": "good match"}),
                content="FIT\ngood match",
                kind="final",
            )],
            supports_structured_output=True,
        )
        await run_stage(job, FakeAgentBackend([]), Stage.fit_assessment, session, fit_backend=fit_backend)

        assert fit_backend.received_schemas == [json_schema_for(Stage.fit_assessment)]
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.fit_done


class TestReplayUnlocksSentinelToStructured:
    """Previously (schema hardcoded to None => structured=False everywhere), a sentinel
    row could only ever be replayed unchanged into another sentinel-mode session. Phase 5
    computes the destination flag from the actual backend, so a BF-19 switch FROM a CLI
    backend TO a structured-capable one must now unwrap sentinel rows into canonical form."""

    async def test_sentinel_history_is_unwrapped_for_a_structured_destination(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        session.add(Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV"))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>",
        ))
        session.add(FollowUp(job_id=job.id, stage=Stage.cv_adjust, question="q?", answer="a!"))
        await session.commit()
        from datetime import datetime
        await session.execute(
            FollowUp.__table__.update().where(FollowUp.job_id == job.id).values(answered_at=datetime.utcnow())
        )
        await session.commit()

        class _CapturingRestoreBackend(FakeAgentBackend):
            def __init__(self, replies):
                super().__init__(replies, supports_structured_output=True)
                self.restore_histories: list[list[HistoryTurn]] = []

            async def restore_session(self, system_prompt, history, external_id, structured_schema=None):
                self.restore_histories.append(list(history))
                return await super().restore_session(system_prompt, history, external_id, structured_schema)

        backend = _CapturingRestoreBackend([_canonical_final(_CV_PAYLOAD)])
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert len(backend.restore_histories) == 1
        assistant_turns = [t for t in backend.restore_histories[0] if t.role == "assistant"]
        assert len(assistant_turns) == 1
        assert assistant_turns[0].content == _canonical_final_str(_CV_PAYLOAD)
        assert backend.received_schemas[0] == json_schema_for(Stage.cv_adjust)


class TestFreshSessionRetryEndToEnd:
    async def test_recovers_after_wire_level_failures_and_completes_the_job(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _FlakyStartBackend([_canonical_final(_CV_PAYLOAD)], fail_times=MAX_FINAL_CORRECTIONS)
        await run_stage(job, backend, Stage.cv_adjust, session)

        assert backend.call_count == MAX_FINAL_CORRECTIONS + 1
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review

    async def test_budget_exhausted_propagates_and_does_not_checkpoint(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        backend = _FlakyStartBackend([], fail_times=99)
        with pytest.raises(ProtocolError):
            await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.running  # never checkpointed past the failure


_CL_PAYLOAD = {
    "salutation": "Dear Hiring Manager,",
    "paragraphs": [
        "I am excited to apply because your mission resonates with my four years of "
        "shipping production systems and developer tooling.",
        "I am confident my background aligns well with what your team needs.",
    ],
    "signoff": "Sincerely,\nCandidate Name",
}


def _canonical_cl_final(payload: dict) -> AgentReply:
    raw = json.dumps({"kind": "final", "question": None, "payload": payload})
    return AgentReply(raw=raw, content=json.dumps(payload), kind="final")


def _canonical_question_str(question: str) -> str:
    return json.dumps({"kind": "question", "question": question, "payload": None})


class TestCoverLetterStructuredWiring:
    """cover_letter is the only stage exercising ClTurn rather than CvTurn end-to-end."""

    async def test_fresh_cover_letter_structured_completes_to_review(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        transition(job, JobState.cv_review, None)
        transition(job, JobState.cv_done, None)  # simulate approve-cv
        transition(job, JobState.running, Stage.cover_letter)
        await session.commit()

        backend = FakeAgentBackend([_canonical_cl_final(_CL_PAYLOAD)], supports_structured_output=True)
        await run_stage(job, backend, Stage.cover_letter, session)

        assert backend.received_schemas == [json_schema_for(Stage.cover_letter)]
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review
        docs = await repo.get_documents(session, job.id, stage=Stage.cover_letter)
        assert "excited to apply" in docs[0].markdown
        assert docs[0].structured is not None


class TestRevisingCvStructuredWiring:
    """The revision call sites got the most edits in Phase 5 (two restore_session
    sites plus two _send_message_with_wire_retry calls) — cover both with a
    structured-capable destination backend."""

    async def _add_revision_request(self, session, job_id, target, instruction="Make it shorter"):
        from jsa.db.models import RevisionRequest

        rr = RevisionRequest(job_id=job_id, target=target, instruction=instruction, consumed_at=None)
        session.add(rr)
        await session.commit()
        return rr

    async def test_fresh_revision_receives_schema_and_completes(self, session):
        job = await _insert_job(session)

        session.add(Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV"))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>",
        ))
        await session.commit()

        await self._add_revision_request(session, job.id, Stage.cv_adjust)
        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        backend = FakeAgentBackend([_canonical_final(_CV_PAYLOAD)], supports_structured_output=True)
        await run_stage(job, backend, Stage.revising_cv, session)

        assert backend.received_schemas == [json_schema_for(Stage.revising_cv)]
        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.review  # NULL origin_state legacy row → review

    async def test_mid_revision_resume_unwraps_sentinel_history_for_structured_destination(self, session):
        """The plan's specifically-called-out gap, now exercised toward the OTHER
        (newly-unlocked) direction: sentinel-shaped rows (as if the original cv_adjust
        AND the revision itself ran on a CLI backend) replayed into a structured
        destination after a BF-19 switch must arrive unwrapped."""
        job = await _insert_job(session)

        session.add(Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="Initial CV"))
        session.add(Message(
            job_id=job.id, stage=Stage.cv_adjust, role="assistant",
            content=f"<<<FINAL>>>\n{json.dumps(_CV_PAYLOAD)}\n<<<END>>>",
        ))
        await session.commit()

        rev_req = await self._add_revision_request(session, job.id, Stage.cv_adjust)

        session.add(Message(job_id=job.id, stage=Stage.revising_cv, role="user", content="Make it shorter"))
        session.add(Message(
            job_id=job.id, stage=Stage.revising_cv, role="assistant",
            content="<<<NEED_INPUT>>>\nShould I drop the oldest role entirely?\n<<<END>>>",
        ))
        await session.commit()

        from datetime import datetime, timedelta

        session.add(FollowUp(
            job_id=job.id, stage=Stage.revising_cv,
            question="Should I drop the oldest role entirely?", answer="Yes, drop it.",
            answered_at=rev_req.created_at + timedelta(seconds=1),
        ))
        await session.commit()

        transition(job, JobState.running, Stage.revising_cv)
        await session.commit()

        class _CapturingRestoreBackend(FakeAgentBackend):
            def __init__(self, replies):
                super().__init__(replies, supports_structured_output=True)
                self.restore_histories: list[list[HistoryTurn]] = []

            async def restore_session(self, system_prompt, history, external_id, structured_schema=None):
                self.restore_histories.append(list(history))
                return await super().restore_session(system_prompt, history, external_id, structured_schema)

        backend = _CapturingRestoreBackend([_canonical_final(_CV_PAYLOAD)])
        await run_stage(job, backend, Stage.revising_cv, session)

        assert len(backend.restore_histories) == 1
        assistant_contents = [t.content for t in backend.restore_histories[0] if t.role == "assistant"]
        assert assistant_contents == [
            _canonical_final_str(_CV_PAYLOAD),
            _canonical_question_str("Should I drop the oldest role entirely?"),
        ]
        assert backend.received_schemas[0] == json_schema_for(Stage.revising_cv)


class TestStructuredSelfHealWording:
    async def test_correction_text_is_structured_worded_not_sentinel_worded(self, session):
        job = await _insert_job(session)
        transition(job, JobState.running, Stage.cv_adjust)
        await session.commit()

        bad_payload = {"contact": {"email": "no-name@x.com"}, "sections": []}  # fails schema

        class _CapturingSendBackend(FakeAgentBackend):
            def __init__(self, replies):
                super().__init__(replies, supports_structured_output=True)
                self.sent_texts: list[str] = []

            async def send_message(self, handle, text, structured_schema=None):
                self.sent_texts.append(text)
                return self._pop_reply()

        backend = _CapturingSendBackend([
            _canonical_final(bad_payload),
            _canonical_final(_CV_PAYLOAD),
        ])
        await run_stage(job, backend, Stage.cv_adjust, session)

        refreshed = await repo.get_job(session, job.id)
        assert refreshed.state == JobState.cv_review
        assert len(backend.sent_texts) == 1
        correction = backend.sent_texts[0]
        assert "<<<" not in correction
        assert "payload" in correction
