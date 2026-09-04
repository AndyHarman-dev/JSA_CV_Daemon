"""Phase 1 of per-job prompt injection: storage + API for `job.injection`.

Covers:
- `PromptInjection.normalized()` — the single normalization point
- `parse_injection` tolerance: a malformed / hand-edited column never raises
- the `injection` field on the job DTO (parsed object, never the raw string)
- `PUT /api/jobs/{id}/injection` — 404, the `queued`-only 400, write, clear
- the column surviving `reset_job` (it rides the Job row; deliberately not reset)

Fixtures mirror `tests/backend/test_api.py` (there is no shared conftest) and use
`FakeAgentBackend` per CLAUDE.md -> "Testing conventions" (fakes over mocks).
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient, ASGITransport

from jsa.config import Settings
from jsa.db import repo
from jsa.db.models import Job, JobState
from jsa.pipeline.orchestrator import Orchestrator
from jsa.schema.injection import PromptInjection, parse_injection
from jsa.server import create_app


# ---------------------------------------------------------------------------
# Fixtures (copied from test_api.py — no conftest exists for tests/backend)
# ---------------------------------------------------------------------------


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    from tests.backend.fakes.fake_backend import FakeAgentBackend

    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backend="claude-cli",
        port=8765,
        no_browser=True,
    )
    return create_app(settings)


@pytest.fixture
async def client(test_app):
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    return test_app.state.session_factory


JOB_ID = "aabbccdd00112233"


def _job_data(job_id: str = JOB_ID) -> dict:
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


async def _insert_job(
    session_factory,
    *,
    state: JobState = JobState.queued,
    injection: str | None = None,
    retry_count: int = 0,
    job_id: str = JOB_ID,
) -> Job:
    """Insert a job and force its state / injection column directly.

    `upsert_job` always creates fresh jobs as `queued`; every state other than that
    (and the raw `injection` blob, which has no repo writer) is set here by hand.
    """
    async with session_factory() as session:
        job = await repo.upsert_job(session, _job_data(job_id))
        await session.commit()
    async with session_factory() as session:
        job = await repo.get_job(session, job_id)
        job.state = state
        job.injection = injection
        job.retry_count = retry_count
        await session.commit()
    async with session_factory() as session:
        return await repo.get_job(session, job_id)


# ===========================================================================
# PromptInjection.normalized() — the single normalization point
# ===========================================================================


class TestNormalization:
    def test_all_blank_is_none(self):
        assert PromptInjection().normalized() is None

    def test_whitespace_only_is_none(self):
        inj = PromptInjection(prefix="   ", postfix="\n\t ", first_msg="  \n")
        assert inj.normalized() is None

    def test_one_real_field_survives(self):
        inj = PromptInjection(prefix="  lead with payments  ", postfix="  ", first_msg="")
        norm = inj.normalized()
        assert norm is not None
        assert norm.prefix == "lead with payments"
        assert norm.postfix == ""
        assert norm.first_msg == ""

    def test_all_three_stripped(self):
        norm = PromptInjection(
            prefix="  a  ", postfix="\nb\n", first_msg="\tc\t"
        ).normalized()
        assert norm is not None
        assert (norm.prefix, norm.postfix, norm.first_msg) == ("a", "b", "c")

    def test_normalized_does_not_mutate_the_original(self):
        inj = PromptInjection(prefix="  a  ")
        inj.normalized()
        assert inj.prefix == "  a  "

    def test_extra_key_is_forbidden(self):
        """The prototype's camelCase `firstMsg` must not silently validate."""
        with pytest.raises(Exception):
            PromptInjection.model_validate({"firstMsg": "x"})

    def test_snake_case_wire_shape(self):
        dumped = json.loads(PromptInjection(first_msg="x").model_dump_json())
        assert set(dumped) == {"prefix", "postfix", "first_msg"}


# ===========================================================================
# parse_injection — must never raise on a hand-edited column
# ===========================================================================


class TestParseInjectionTolerance:
    @pytest.mark.parametrize(
        "raw",
        [
            "{not json",           # the plan's named case
            "",                    # empty string
            "   ",                 # whitespace
            "[1,2]",               # valid JSON, wrong container
            "null",
            "5",
            '"a string"',
            '{"firstMsg": "x"}',   # prototype camelCase — ValidationError, not a decode error
            '{"prefix": 5}',       # wrong field type
            '{"prefix": "a", "bogus": "b"}',  # extra="forbid"
        ],
    )
    def test_malformed_reads_as_none_without_raising(self, raw):
        assert parse_injection(raw) is None

    def test_none_column_is_none(self):
        assert parse_injection(None) is None

    def test_valid_blob_round_trips(self):
        raw = PromptInjection(prefix="p", postfix="s", first_msg="f").model_dump_json()
        parsed = parse_injection(raw)
        assert parsed == PromptInjection(prefix="p", postfix="s", first_msg="f")

    def test_partial_blob_fills_defaults(self):
        parsed = parse_injection('{"prefix": "only"}')
        assert parsed is not None
        assert (parsed.prefix, parsed.postfix, parsed.first_msg) == ("only", "", "")

    def test_stored_blank_blob_normalizes_to_none(self):
        """A stored all-whitespace triple reads back as 'no injection'.

        parse_injection is the read side of the single normalization point — read
        sites must not have to re-strip.
        """
        assert parse_injection('{"prefix": "  ", "postfix": "", "first_msg": "\\n"}') is None

    def test_parse_strips(self):
        parsed = parse_injection('{"prefix": "  p  "}')
        assert parsed is not None
        assert parsed.prefix == "p"


# ===========================================================================
# DTO round-trip
# ===========================================================================


class TestJobDtoField:
    async def test_no_injection_is_null_on_the_dto(self, client, db):
        await _insert_job(db)
        resp = await client.get(f"/api/jobs/{JOB_ID}")
        assert resp.status_code == 200
        assert resp.json()["injection"] is None

    async def test_stored_injection_is_exposed_as_a_parsed_object(self, client, db):
        raw = PromptInjection(prefix="p", postfix="s", first_msg="f").model_dump_json()
        await _insert_job(db, injection=raw)

        resp = await client.get(f"/api/jobs/{JOB_ID}")
        assert resp.status_code == 200
        got = resp.json()["injection"]
        assert got == {"prefix": "p", "postfix": "s", "first_msg": "f"}

    async def test_injection_present_on_the_list_dto_too(self, client, db):
        """The job LIST (full=False) drives the syringe indicator, so the field
        must live in the base dict, not behind `if full:`."""
        raw = PromptInjection(prefix="p").model_dump_json()
        await _insert_job(db, injection=raw)

        resp = await client.get("/api/jobs")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["injection"] == {"prefix": "p", "postfix": "", "first_msg": ""}

    async def test_malformed_column_degrades_to_null_on_the_dto(self, client, db):
        await _insert_job(db, injection="{not json")
        resp = await client.get(f"/api/jobs/{JOB_ID}")
        assert resp.status_code == 200
        assert resp.json()["injection"] is None


# ===========================================================================
# PUT /api/jobs/{id}/injection
# ===========================================================================


class TestPutInjection:
    async def test_unknown_job_is_404(self, client, db):
        resp = await client.put(
            "/api/jobs/deadbeefdeadbeef/injection", json={"prefix": "x"}
        )
        assert resp.status_code == 404

    async def test_write_on_queued_job(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(
            f"/api/jobs/{JOB_ID}/injection",
            json={"prefix": "lead with payments", "postfix": "never hedge", "first_msg": "6y Rust"},
        )
        assert resp.status_code == 200
        assert resp.json()["injection"] == {
            "prefix": "lead with payments",
            "postfix": "never hedge",
            "first_msg": "6y Rust",
        }

        # and it persisted
        resp = await client.get(f"/api/jobs/{JOB_ID}")
        assert resp.json()["injection"]["prefix"] == "lead with payments"

    async def test_write_stores_stripped_values(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(
            f"/api/jobs/{JOB_ID}/injection", json={"prefix": "  padded  "}
        )
        assert resp.status_code == 200
        assert resp.json()["injection"]["prefix"] == "padded"

        async with db() as session:
            job = await repo.get_job(session, JOB_ID)
            assert json.loads(job.injection)["prefix"] == "padded"

    async def test_partial_body_defaults_the_rest(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(f"/api/jobs/{JOB_ID}/injection", json={"postfix": "p"})
        assert resp.status_code == 200
        assert resp.json()["injection"] == {"prefix": "", "postfix": "p", "first_msg": ""}

    @pytest.mark.parametrize(
        "state", [JobState.pending, JobState.running, JobState.approved]
    )
    async def test_non_queued_is_400_naming_the_state(self, client, db, state):
        await _insert_job(db, state=state)
        resp = await client.put(f"/api/jobs/{JOB_ID}/injection", json={"prefix": "x"})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert state.value in detail
        assert "expected 'queued'" in detail

    async def test_400_does_not_write(self, client, db):
        await _insert_job(db, state=JobState.running)
        await client.put(f"/api/jobs/{JOB_ID}/injection", json={"prefix": "x"})
        async with db() as session:
            job = await repo.get_job(session, JOB_ID)
            assert job.injection is None

    async def test_all_blank_body_clears_an_existing_injection(self, client, db):
        raw = PromptInjection(prefix="p", postfix="s", first_msg="f").model_dump_json()
        await _insert_job(db, state=JobState.queued, injection=raw)

        resp = await client.put(
            f"/api/jobs/{JOB_ID}/injection",
            json={"prefix": "", "postfix": "", "first_msg": ""},
        )
        assert resp.status_code == 200
        assert resp.json()["injection"] is None

        async with db() as session:
            job = await repo.get_job(session, JOB_ID)
            assert job.injection is None  # SQL NULL, not "{}"

    async def test_whitespace_only_body_also_clears(self, client, db):
        raw = PromptInjection(prefix="p").model_dump_json()
        await _insert_job(db, state=JobState.queued, injection=raw)

        resp = await client.put(
            f"/api/jobs/{JOB_ID}/injection",
            json={"prefix": "  ", "postfix": "\n", "first_msg": " \t "},
        )
        assert resp.status_code == 200
        assert resp.json()["injection"] is None
        async with db() as session:
            job = await repo.get_job(session, JOB_ID)
            assert job.injection is None

    async def test_empty_body_is_accepted_as_all_blank(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(f"/api/jobs/{JOB_ID}/injection", json={})
        assert resp.status_code == 200
        assert resp.json()["injection"] is None

    async def test_camel_case_key_is_rejected(self, client, db):
        """`extra="forbid"` — the wire shape is snake_case only."""
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(f"/api/jobs/{JOB_ID}/injection", json={"firstMsg": "x"})
        assert resp.status_code == 422

    async def test_response_is_the_full_job_dict(self, client, db):
        await _insert_job(db, state=JobState.queued)
        resp = await client.put(f"/api/jobs/{JOB_ID}/injection", json={"prefix": "x"})
        body = resp.json()
        assert body["id"] == JOB_ID
        assert body["state"] == "queued"  # no state transition happened
        assert "documents" in body and "follow_ups" in body

    async def test_write_does_not_transition_state(self, client, db):
        await _insert_job(db, state=JobState.queued)
        await client.put(f"/api/jobs/{JOB_ID}/injection", json={"prefix": "x"})
        async with db() as session:
            job = await repo.get_job(session, JOB_ID)
            assert job.state == JobState.queued
            assert job.current_stage is None


# ===========================================================================
# The column rides the Job row through resets (deliberately not reset)
# ===========================================================================


class TestInjectionSurvivesReset:
    async def test_soft_reset_keeps_the_injection(self, client, db):
        raw = PromptInjection(prefix="keep me", first_msg="and me").model_dump_json()
        # reset_job requires failed/dismissed, so the column is seeded directly —
        # the PUT itself is queued-only.
        await _insert_job(db, state=JobState.failed, injection=raw, retry_count=0)

        resp = await client.post(f"/api/jobs/{JOB_ID}/reset")
        assert resp.status_code == 200
        assert resp.json()["injection"] == {
            "prefix": "keep me",
            "postfix": "",
            "first_msg": "and me",
        }

    async def test_nuclear_reset_keeps_the_injection(self, client, db):
        raw = PromptInjection(prefix="keep me").model_dump_json()
        await _insert_job(db, state=JobState.failed, injection=raw, retry_count=1)

        resp = await client.post(f"/api/jobs/{JOB_ID}/reset")
        assert resp.status_code == 200
        assert resp.json()["injection"]["prefix"] == "keep me"

    async def test_dismissed_reset_keeps_the_injection(self, client, db):
        raw = PromptInjection(postfix="keep me").model_dump_json()
        await _insert_job(db, state=JobState.dismissed, injection=raw)

        resp = await client.post(f"/api/jobs/{JOB_ID}/reset")
        assert resp.status_code == 200
        assert resp.json()["injection"]["postfix"] == "keep me"


# ===========================================================================
# Migration: the additive ALTER TABLE is idempotent
# ===========================================================================


class TestMigration:
    async def test_init_db_is_idempotent_for_the_injection_column(self, tmp_path):
        from jsa.db.engine import create_engine, init_db

        engine = create_engine(tmp_path / "m.sqlite")
        await init_db(engine)
        await init_db(engine)  # second run must not raise on the existing column

        from sqlalchemy import text as sa_text

        async with engine.begin() as conn:
            cols = (await conn.execute(sa_text("PRAGMA table_info(jobs)"))).fetchall()
        assert "injection" in {row[1] for row in cols}
        await engine.dispose()


# ===========================================================================
# Phase 2: assemble_system_prompt threading
#
# The injection brackets the file-authored prompt_text and NOTHING else — every
# machine-authored runtime section (structured contract, language directive,
# current-date directive) keeps its existing FINAL position, AFTER the postfix.
# ===========================================================================


from datetime import datetime  # noqa: E402

from jsa.db.models import Stage  # noqa: E402
from jsa.pipeline.prompt_assembly import assemble_system_prompt  # noqa: E402
from jsa.pipeline.stages import _build_fit_user_msg, _build_initial_user_msg  # noqa: E402
from jsa.schema.turn_models import json_schema_for  # noqa: E402

_PROMPT_TEXT = "SYSTEM PROMPT BODY — the file-authored stage instructions."
_NOW = datetime(2026, 1, 2, 3, 4, 5)


class TestAssemblyBrackets:
    def test_prefix_lands_before_the_prompt_text(self):
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="en",
            injection=PromptInjection(prefix="LEAD WITH PAYMENTS"),
        )
        assert out.index("LEAD WITH PAYMENTS") < out.index(_PROMPT_TEXT)
        assert out == f"LEAD WITH PAYMENTS\n\n{_PROMPT_TEXT}"

    def test_postfix_lands_after_the_prompt_text(self):
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="en",
            injection=PromptInjection(postfix="KEEP IT TO ONE PAGE"),
        )
        assert out.index(_PROMPT_TEXT) < out.index("KEEP IT TO ONE PAGE")
        assert out == f"{_PROMPT_TEXT}\n\nKEEP IT TO ONE PAGE"

    def test_both_bracket_the_prompt_text(self):
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="en",
            injection=PromptInjection(prefix="BEFORE", postfix="AFTER"),
        )
        assert out == f"BEFORE\n\n{_PROMPT_TEXT}\n\nAFTER"

    def test_first_msg_only_never_touches_the_system_prompt(self):
        """normalized() is non-None here (first_msg survives), but neither prefix nor
        postfix does — so the assembled prompt must stay byte-identical. This is the
        one path where "an injection is present" and "the system prompt is unchanged"
        must both hold."""
        baseline = assemble_system_prompt(_PROMPT_TEXT, language="en")
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="en",
            injection=PromptInjection(first_msg="mention the open-source work"),
        )
        assert out == baseline


class TestPostfixNeverOutranksTheMachineAuthoredSections:
    """Ordering is load-bearing, not cosmetic: the structured contract asserts it
    "supersedes any sentinel-block instructions above", so a postfix landing after it
    (e.g. "reply in plain prose, no JSON") would become the last word over the JSON
    contract — a ProtocolError every turn, burning the MAX_FINAL_CORRECTIONS self-heal
    budget and then triggering a BF-19 backend hop."""

    def _assembled(self) -> str:
        return assemble_system_prompt(
            _PROMPT_TEXT,
            language="es",
            structured_model=json_schema_for(Stage.cv_adjust),
            now=_NOW,
            injection=PromptInjection(prefix="BEFORE", postfix="AFTER"),
        )

    def test_postfix_precedes_the_structured_contract(self):
        out = self._assembled()
        assert out.index("AFTER") < out.index("## Structured output contract")

    def test_postfix_precedes_the_language_directive(self):
        out = self._assembled()
        assert out.index("AFTER") < out.index("## Output language")

    def test_postfix_precedes_the_current_date_directive(self):
        out = self._assembled()
        assert out.index("AFTER") < out.index("## Current date")

    def test_full_relative_order_prefix_prompt_postfix_then_machine_sections(self):
        out = self._assembled()
        positions = [
            out.index("BEFORE"),
            out.index(_PROMPT_TEXT),
            out.index("AFTER"),
            out.index("## Structured output contract"),
            out.index("## Output language"),
            out.index("## Current date"),
        ]
        assert positions == sorted(positions)


class TestByteIdentityWithoutAnInjection:
    """injection=None and an all-blank injection must both produce output
    byte-identical to the pre-injection implementation — the cross-job prompt-cache
    prefix invariant (CLAUDE.md → "Prompt caching") depends on it."""

    _BLANKS = [
        PromptInjection(),
        PromptInjection(prefix="   ", postfix="\n\t ", first_msg="  \n"),
    ]

    @pytest.mark.parametrize("stage_name", ["fit_assessment", "cv_adjust", "cover_letter"])
    @pytest.mark.parametrize("structured", [False, True])
    @pytest.mark.parametrize("for_resume", [False, True])
    def test_none_and_blank_match_the_no_injection_output(
        self, stage_name, structured, for_resume
    ):
        from jsa.prompts.loader import read_prompt

        stage = Stage[stage_name]
        kwargs = dict(
            language="es",
            structured_model=json_schema_for(stage) if structured else None,
            fit_verdict=stage is Stage.fit_assessment,
            for_resume=for_resume,
            now=_NOW,
        )
        prompt_text = read_prompt(stage_name)
        baseline = assemble_system_prompt(prompt_text, **kwargs)

        assert assemble_system_prompt(prompt_text, injection=None, **kwargs) == baseline
        for blank in self._BLANKS:
            assert assemble_system_prompt(prompt_text, injection=blank, **kwargs) == baseline


class TestSentinelResumeKeepsTheWrapper:
    """The sentinel-mode for_resume=True early return must return the WRAPPED text.
    Returning the bare prompt_text there silently drops the user's wrapper from every
    turn after the first on a CLI backend rebuilt from history."""

    def test_for_resume_sentinel_mode_carries_prefix_and_postfix(self):
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="es",
            structured_model=None,
            for_resume=True,
            now=_NOW,
            injection=PromptInjection(prefix="BEFORE", postfix="AFTER"),
        )
        assert out == f"BEFORE\n\n{_PROMPT_TEXT}\n\nAFTER"
        # Still no language/date directive on this path — unchanged from before.
        assert "## Output language" not in out
        assert "## Current date" not in out

    def test_for_resume_structured_mode_carries_prefix_and_postfix(self):
        out = assemble_system_prompt(
            _PROMPT_TEXT,
            language="en",
            structured_model=json_schema_for(Stage.cv_adjust),
            for_resume=True,
            injection=PromptInjection(prefix="BEFORE", postfix="AFTER"),
        )
        assert out.index("BEFORE") < out.index(_PROMPT_TEXT) < out.index("AFTER")
        assert out.index("AFTER") < out.index("## Structured output contract")


# ===========================================================================
# Phase 2: the first user message
# ===========================================================================


def _plain_job() -> Job:
    return Job(**_job_data(), state=JobState.pending)


class TestInitialUserMsgFirstMsg:
    def test_first_msg_is_appended_last_under_the_label(self):
        job = _plain_job()
        msg = _build_initial_user_msg(job, None, None, first_msg="mention the OSS work")
        assert msg.endswith(
            "ADDITIONAL INSTRUCTIONS FROM THE USER (apply these to your work on this "
            "application):\nmention the OSS work"
        )
        assert msg.index(job.jd) < msg.index("ADDITIONAL INSTRUCTIONS FROM THE USER")
        assert msg.index(f"TIER: {job.tier}") < msg.index("ADDITIONAL INSTRUCTIONS FROM THE USER")

    @pytest.mark.parametrize("first_msg", [None, ""])
    def test_absent_first_msg_is_byte_identical(self, first_msg):
        job = _plain_job()
        baseline = _build_initial_user_msg(job, None, None)
        assert _build_initial_user_msg(job, None, None, first_msg=first_msg) == baseline
        assert baseline.endswith(f"TIER: {job.tier}")

    def test_first_msg_composes_with_brief_and_cv_block(self):
        job = _plain_job()
        msg = _build_initial_user_msg(
            job, "[INTEL_BRIEF] stuff", ("BASE CV STRUCTURE", "{}"), first_msg="do X"
        )
        positions = [
            msg.index("[INTEL_BRIEF]"),
            msg.index("BASE CV STRUCTURE"),
            msg.index("JOB DESCRIPTION"),
            msg.index("TIER:"),
            msg.index("ADDITIONAL INSTRUCTIONS FROM THE USER"),
        ]
        assert positions == sorted(positions)


class TestFitUserMsgIsNotTouched:
    """Locked decision: the fit gate honors prefix/postfix (system prompt) but never
    first_msg — _build_fit_user_msg stays a pure function of (job, base_structure)."""

    def test_signature_takes_no_injection(self):
        import inspect

        sig = inspect.signature(_build_fit_user_msg)
        assert list(sig.parameters) == ["job", "base_structure"]

    def test_output_carries_no_user_instruction_block(self):
        job = _plain_job()
        msg = _build_fit_user_msg(job, base_structure=None)
        assert "ADDITIONAL INSTRUCTIONS FROM THE USER" not in msg
        assert msg.endswith(f"JOB DESCRIPTION:\n{job.jd}")


# ===========================================================================
# Phase 2: run_stage threading (fakes only — CLAUDE.md → "Testing conventions")
# ===========================================================================


from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from jsa.agents.base import AgentReply  # noqa: E402
from jsa.db.models import Base  # noqa: E402
from jsa.pipeline.stages import run_stage  # noqa: E402
from jsa.pipeline.state_machine import transition  # noqa: E402


@pytest.fixture
async def pipeline_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_job(session: AsyncSession, injection_raw: str) -> Job:
    job = await repo.upsert_job(session, _job_data())
    job.state = JobState.pending
    job.injection = injection_raw
    await session.commit()
    return job


def _capturing_backend_cls():
    from uuid import uuid4

    from tests.backend.fakes.fake_backend import FakeAgentBackend, FakeSessionHandle

    class _Capturing(FakeAgentBackend):
        def __init__(self, replies):
            super().__init__(replies)
            self.start_session_prompt: str | None = None
            self.start_session_user_msg: str | None = None
            self.restore_session_prompt: str | None = None

        async def start_session(self, system_prompt, initial_user_msg):
            self.start_session_prompt = system_prompt
            self.start_session_user_msg = initial_user_msg
            return FakeSessionHandle(id=str(uuid4()), external_id=None), self._pop_reply()

        async def restore_session(self, system_prompt, history, external_id):
            self.restore_session_prompt = system_prompt
            return FakeSessionHandle(id=str(uuid4()), external_id=external_id)

    return _Capturing


_CV_JSON = {
    "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
    "sections": [{"name": "Summary", "text": "Adjusted CV: senior engineer."}],
}


def _final_reply(content: str) -> "AgentReply":
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


_INJECTION_RAW = json.dumps(
    {"prefix": "PREFIX-MARKER", "postfix": "POSTFIX-MARKER", "first_msg": "FIRSTMSG-MARKER"}
)


class TestRunStageThreadsTheInjection:
    async def test_fresh_cv_adjust_session_carries_prefix_postfix_and_first_msg(
        self, pipeline_session
    ):
        job = await _seed_job(pipeline_session, _INJECTION_RAW)
        transition(job, JobState.running, Stage.cv_adjust)
        await pipeline_session.commit()

        backend = _capturing_backend_cls()([_final_reply(json.dumps(_CV_JSON))])
        await run_stage(job, backend, Stage.cv_adjust, pipeline_session)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert prompt.startswith("PREFIX-MARKER\n\n")
        # The postfix brackets the prompt file but still precedes every
        # machine-authored section (here: the current-date directive).
        assert "\n\nPOSTFIX-MARKER" in prompt
        assert prompt.index("POSTFIX-MARKER") < prompt.index("## Current date")
        assert backend.start_session_user_msg.endswith(
            "ADDITIONAL INSTRUCTIONS FROM THE USER (apply these to your work on this "
            "application):\nFIRSTMSG-MARKER"
        )

    async def test_resumed_cv_adjust_session_still_carries_prefix_and_postfix(
        self, pipeline_session
    ):
        """Every structured-capable backend is wire-stateless and resends the system
        prompt on every HTTP call; a CLI backend restores from history. Miss
        resume_system_prompt and the injection applies to turn 1 and evaporates from
        turn 2 onward."""
        from datetime import datetime as _dt

        from jsa.db.models import FollowUp, Message

        job = await _seed_job(pipeline_session, _INJECTION_RAW)
        transition(job, JobState.running, Stage.cv_adjust)
        pipeline_session.add(
            Message(job_id=job.id, stage=Stage.cv_adjust, role="user", content="hi")
        )
        pipeline_session.add(
            Message(job_id=job.id, stage=Stage.cv_adjust, role="assistant", content="ok")
        )
        pipeline_session.add(
            FollowUp(
                job_id=job.id,
                stage=Stage.cv_adjust,
                question="q?",
                answer="a!",
                answered_at=_dt.utcnow(),
            )
        )
        await pipeline_session.commit()

        backend = _capturing_backend_cls()([_final_reply(json.dumps(_CV_JSON))])
        await run_stage(job, backend, Stage.cv_adjust, pipeline_session)

        assert backend.start_session_prompt is None  # resume path
        prompt = backend.restore_session_prompt
        assert prompt is not None
        assert prompt.startswith("PREFIX-MARKER\n\n")
        assert prompt.endswith("\n\nPOSTFIX-MARKER")

    async def test_no_injection_leaves_both_paths_untouched(self, pipeline_session):
        job = await _seed_job(pipeline_session, None)
        transition(job, JobState.running, Stage.cv_adjust)
        await pipeline_session.commit()

        backend = _capturing_backend_cls()([_final_reply(json.dumps(_CV_JSON))])
        await run_stage(job, backend, Stage.cv_adjust, pipeline_session)

        assert "PREFIX-MARKER" not in backend.start_session_prompt
        assert "ADDITIONAL INSTRUCTIONS FROM THE USER" not in backend.start_session_user_msg

    async def test_fit_assessment_gets_prefix_postfix_but_never_first_msg(
        self, pipeline_session
    ):
        job = await _seed_job(pipeline_session, _INJECTION_RAW)
        transition(job, JobState.running, Stage.fit_assessment)
        await pipeline_session.commit()

        backend = _capturing_backend_cls()([_final_reply("FIT\ngood match")])
        await run_stage(job, backend, Stage.fit_assessment, pipeline_session)

        prompt = backend.start_session_prompt
        assert prompt is not None
        assert prompt.startswith("PREFIX-MARKER\n\n")
        assert "\n\nPOSTFIX-MARKER" in prompt
        assert prompt.index("POSTFIX-MARKER") < prompt.index("## Current date")
        # Locked decision: _build_fit_user_msg is not touched.
        assert "FIRSTMSG-MARKER" not in backend.start_session_user_msg
        assert "ADDITIONAL INSTRUCTIONS FROM THE USER" not in backend.start_session_user_msg
