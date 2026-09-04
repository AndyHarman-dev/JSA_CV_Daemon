"""Parity gate for the CV-decks migration (CLAUDE.md → "Parity gate for replace / delete
refactors"; plan Phase 4).

The oracle is the **pre-change behavior of a legacy install**: a single
``cv_structure.json`` on disk, no deck index. Under decks, that install is migrated by
*copying* the legacy file into ``cv_decks/<id>.json`` and resolving that copy back into
``run_stage(cv_structure_path=...)`` — so the two prompts that carry base-CV content

  * the ``fit_assessment`` user message, and
  * the ``cv_adjust`` initial user message

must come out **byte-identical** to what ``main`` produced. The golden strings in
``fixtures/cv_decks_parity/`` were generated on ``main`` (e9c278a) *before* any decks code
existed; regenerating them from post-change code would pin nothing.

Two halves, deliberately:

``TestStagePromptsUnchanged`` drives ``run_stage`` with a bare legacy path and runs
identically before and after the migration — it pins that ``stages.py`` (untouched by
design, plan decision 1) never drifts.

``TestLegacyInstallResolvesToIdenticalPrompt`` drives the same assertions through
``cv_decks.resolve_path``, i.e. the real production seam after Phase 4. It is the half
that actually proves migration parity, and it is skipped until the deck store exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply
from jsa.db import repo
from jsa.db.models import Base, Job, JobState, Stage
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import CapturingBackend

FIXTURES = Path(__file__).parent / "fixtures" / "cv_decks_parity"

# The oracle input. Frozen here (not imported from test_stages.py) so an unrelated edit to
# that file's shared helpers can never silently redefine what parity is measured against.
LEGACY_CV = {
    "contact": {
        "name": "Jane Doe",
        "email": "jane.doe@example.com",
        "phone": "+1-555-867-5309",
        "location": "Berlin, DE",
    },
    "sections": [
        {"name": "Summary", "text": "Backend engineer with a decade on distributed systems."},
        {
            "name": "Experience",
            "entries": [
                {
                    "role": "Senior Engineer",
                    "company": "Acme",
                    "dates": "2019-present",
                    "bullets": [
                        "Built a distributed payment pipeline",
                        "Cut p95 checkout latency by 40%",
                    ],
                },
            ],
        },
        {"name": "Open Source Leadership", "items": ["Maintainer of Foo"]},
        {"name": "Skills", "items": ["Python", "Go", "PostgreSQL"]},
    ],
}

JOB = dict(
    id="aabbccdd00112233",
    company="Acme",
    role="Engineer",
    link="https://acme.com/job",
    tier="A",
    jd="Job description text",
    jd_hash="hash0000deadbeef",
    cv_text="Curriculum vitae text",
)


@pytest.fixture
async def session():
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


@pytest.fixture
def legacy_structure(tmp_path: Path) -> Path:
    """A pre-decks install: exactly one ``cv_structure.json``, nothing else."""
    path = tmp_path / "cv_structure.json"
    path.write_text(json.dumps(LEGACY_CV), encoding="utf-8")
    return path


def _raw_final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


async def _insert_job(session: AsyncSession) -> Job:
    job = await repo.upsert_job(session, dict(JOB))
    job.state = JobState.pending
    await session.commit()
    return job


async def capture_fit_msg(session: AsyncSession, cv_path: Path | None) -> str:
    """The fit_assessment user message produced for `cv_path`."""
    job = await _insert_job(session)
    transition(job, JobState.running, Stage.fit_assessment)
    await session.commit()
    backend = CapturingBackend([_raw_final("FIT\nStrong overlap with the JD.")])
    await run_stage(job, backend, Stage.fit_assessment, session, cv_structure_path=cv_path)
    assert backend.captured_initial_msg is not None
    return backend.captured_initial_msg


async def capture_cv_adjust_msg(session: AsyncSession, cv_path: Path | None) -> str:
    """The cv_adjust initial user message produced for `cv_path`."""
    job = await _insert_job(session)
    transition(job, JobState.running, Stage.cv_adjust)
    await session.commit()
    backend = CapturingBackend([_raw_final(json.dumps(LEGACY_CV))])
    await run_stage(job, backend, Stage.cv_adjust, session, cv_structure_path=cv_path)
    assert backend.captured_initial_msg is not None
    return backend.captured_initial_msg


class TestStagePromptsUnchanged:
    """stages.py is untouched by the decks work (plan decision 1) — pin that."""

    async def test_fit_user_message_matches_golden(self, session, legacy_structure):
        golden = (FIXTURES / "fit_user_msg.txt").read_text(encoding="utf-8")
        assert await capture_fit_msg(session, legacy_structure) == golden

    async def test_cv_adjust_user_message_matches_golden(self, session, legacy_structure):
        golden = (FIXTURES / "cv_adjust_user_msg.txt").read_text(encoding="utf-8")
        assert await capture_cv_adjust_msg(session, legacy_structure) == golden


class TestLegacyInstallResolvesToIdenticalPrompt:
    """The half that proves *migration* parity: a legacy install, migrated to a deck and
    resolved back through the production seam, must reproduce the same two messages.

    Skipped until Phase 1 lands the deck store — the whole point of this file is that it
    exists, with its goldens, before any decks code does.
    """

    @pytest.fixture
    def settings_for_legacy(self, tmp_path: Path, legacy_structure: Path):
        from jsa.config import Settings

        # cv_structure_path / cv_decks_* are all derived from db_path.parent, so pointing
        # db_path into tmp_path puts the legacy file exactly where the migration looks.
        assert legacy_structure.parent == tmp_path
        return Settings(db_path=tmp_path / "jsa.sqlite")

    async def test_migrated_legacy_deck_reproduces_fit_message(self, session, settings_for_legacy):
        cv_decks = pytest.importorskip(
            "jsa.store.cv_decks", reason="deck store lands in Phase 1"
        )
        await cv_decks.load_index(settings_for_legacy)  # triggers migrate_legacy
        resolved = await cv_decks.resolve_path(settings_for_legacy, None)
        assert resolved is not None and resolved != settings_for_legacy.cv_structure_path
        golden = (FIXTURES / "fit_user_msg.txt").read_text(encoding="utf-8")
        assert await capture_fit_msg(session, resolved) == golden

    async def test_migrated_legacy_deck_reproduces_cv_adjust_message(
        self, session, settings_for_legacy
    ):
        cv_decks = pytest.importorskip(
            "jsa.store.cv_decks", reason="deck store lands in Phase 1"
        )
        await cv_decks.load_index(settings_for_legacy)
        resolved = await cv_decks.resolve_path(settings_for_legacy, None)
        assert resolved is not None
        golden = (FIXTURES / "cv_adjust_user_msg.txt").read_text(encoding="utf-8")
        assert await capture_cv_adjust_msg(session, resolved) == golden

    async def test_legacy_file_is_left_on_disk_untouched(self, settings_for_legacy):
        cv_decks = pytest.importorskip(
            "jsa.store.cv_decks", reason="deck store lands in Phase 1"
        )
        legacy = settings_for_legacy.cv_structure_path
        before = legacy.read_bytes()
        await cv_decks.load_index(settings_for_legacy)
        assert legacy.exists(), "plan decision 4: the legacy file is never deleted"
        assert legacy.read_bytes() == before, "plan decision 4: never rewritten either"
