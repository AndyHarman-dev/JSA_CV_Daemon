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
