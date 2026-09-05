"""Cross-feature gates for the three features merged onto this branch.

`feat/revision-tool-use`, `feat/cv-decks` and `feat/prompt-injection` were built in
three separate worktrees off the same `main`. Every test each of them shipped runs
against a tree containing exactly ONE of them, so no branch's suite can see an
interaction with another — and a bad merge stays green in all three.

That is not hypothetical. Reintroducing the single most plausible mis-merge (the tool
branch of `assemble_system_prompt` returning `prompt_text` instead of the
injection-wrapped `base`) leaves all 2210 pre-merge tests passing; only the gates added
here and in `test_prompt_assembly.py::TestToolContractCarriesTheInjection` fail.

So this file is the merge's actual deliverable. Everything in it asserts a claim no
single branch could have made.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentReply, HistoryTurn, ToolCall
from jsa.agents.tool_spec import ToolSpec
from jsa.config import Settings
from jsa.db import repo
from jsa.db.engine import init_db
from jsa.db.models import Base, Document, Job, JobState, RevisionRequest, Stage
from jsa.pipeline.orchestrator import Orchestrator
from jsa.pipeline.stages import run_stage
from jsa.pipeline.state_machine import transition
from jsa.schema import CVDocument
from jsa.schema.injection import PromptInjection
from jsa.server import make_base_cv_resolver
from jsa.store import cv_decks
from tests.backend.fakes.fake_backend import FakeAgentBackend


# --------------------------------------------------------------------------- helpers

def _cv(name: str, summary: str) -> dict:
    return {
        "contact": {"name": name, "email": "x@x.com"},
        "sections": [
            {"name": "Summary", "text": summary},
            {"name": "Skills", "items": ["Python", "Go"]},
        ],
    }


def _final(content: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


def _tool_calls(calls: list[tuple[str, dict]]) -> AgentReply:
    """A tool_calls reply in the shape a backend hands to run_tool_loop (same helper
    shape as tests/backend/test_patch_parity.py)."""
    return AgentReply(
        raw="tool_calls",
        content="tool_calls",
        kind="tool_calls",
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(calls)
        ],
    )


def _structured_final(payload: dict) -> AgentReply:
    content = json.dumps(payload)
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


class PromptCapturingBackend(FakeAgentBackend):
    """Records every system prompt AND initial user message this backend is handed.

    Both `start_session` and `restore_session` are captured, because the two features
    whose interaction is under test land on different call sites: the deck goes into the
    *user* message of a fresh session, the injection into the *system* prompt of every
    session including a resumed tool-mode one.
    """

    def __init__(self, replies: list[AgentReply], **kw: Any) -> None:
        super().__init__(replies, **kw)
        self.system_prompts: list[str] = []
        self.user_msgs: list[str] = []

    async def start_session(self, system_prompt: str, initial_user_msg: str, *a: Any, **kw: Any):
        self.system_prompts.append(system_prompt)
        self.user_msgs.append(initial_user_msg)
        return await super().start_session(system_prompt, initial_user_msg, *a, **kw)

    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
        structured_schema: dict[str, Any] | None = None,
        tools: tuple[ToolSpec, ...] | None = None,
    ):
        self.system_prompts.append(system_prompt)
        return await super().restore_session(
            system_prompt, history, external_id, structured_schema, tools
        )


_PREFIX = "HOUSE STYLE: never use the word 'synergy'."
_POSTFIX = "Always close with a one-line rationale."
_FIRST_MSG = "This company runs Rust in production; lead with the Rust work."


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
def settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "jsa.sqlite")


async def _insert_job(session: AsyncSession, job_id: str, **fields: Any) -> Job:
    job = await repo.upsert_job(
        session,
        dict(
            id=job_id,
            company="Acme",
            role="Engineer",
            link="https://acme.example/job",
            tier="A",
            jd="We need a backend engineer.",
            jd_hash="hash0000deadbeef",
        ),
    )
    for k, v in fields.items():
        setattr(job, k, v)
    await session.commit()
    return job


# ------------------------------------------- 1. injection x tool-mode revision (stages)

class TestInjectionSurvivesAToolModeRevision:
    """`feat/prompt-injection` x `feat/revision-tool-use`, at the `run_stage` seam.

    revision-tool-use adds a FOURTH `assemble_system_prompt` call site — the
    `_tool_system_prompt` callback that feeds `run_tool_loop`. prompt-injection threads
    `injection=` into the three call sites that existed on its own branch and could not
    know about this one. `stages.py` auto-merged with NO conflict, so the omission was
    silent: on the prompt rung that system prompt is the only transport the model gets,
    and a revision on an injected job would quietly run unwrapped.
    """

    _ORIGINAL = "Backend engineer with six years across payments and infrastructure."
    _REVISED = "Payments-focused backend engineer, six years, infrastructure depth."

    @property
    def _cv_payload(self) -> dict:
        return {
            "contact": {"name": "Jane Doe", "email": "jane.doe@example.com"},
            "sections": [
                {"name": "Summary", "text": self._ORIGINAL},
                {
                    "name": "Experience",
                    "entries": [
                        {
                            "heading": "Senior Engineer",
                            "subheading": "Acme",
                            "dates": "2019-present",
                            "bullets": ["Built a distributed payment pipeline"],
                        }
                    ],
                },
            ],
        }

    async def _run_revision(self, session_factory, injection: PromptInjection | None):
        """Seed a real cv_adjust (so the revision has Message history AND a Document with
        a populated `.structured` to build the working copy from), park at cv_review, then
        run the revision through the tool loop."""
        async with session_factory() as session:
            job = await _insert_job(
                session,
                uuid4().hex[:16],
                state=JobState.pending,
                injection=injection.model_dump_json() if injection else None,
            )
            transition(job, JobState.running, Stage.cv_adjust)
            await session.commit()

            seed = PromptCapturingBackend([_structured_final(self._cv_payload)])
            await run_stage(job, seed, Stage.cv_adjust, session)
            job = await repo.get_job(session, job.id)
            assert job.state == JobState.cv_review

            session.add(
                RevisionRequest(
                    job_id=job.id,
                    target=Stage.cv_adjust,
                    instruction="Tighten the summary and lead with payments.",
                    origin_state="cv_review",
                )
            )
            await session.commit()

            backend = PromptCapturingBackend([
                _tool_calls([("get_cv", {})]),
                _tool_calls([("replace_summary", {"text": self._REVISED})]),
                _tool_calls([("finalize", {"change_log": "Rewrote the summary."})]),
            ])
            transition(job, JobState.running, Stage.revising_cv)
            await session.commit()
            await run_stage(job, backend, Stage.revising_cv, session)

            docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
            return backend, seed, max(docs, key=lambda d: d.version)

    async def test_the_tool_session_system_prompt_carries_prefix_and_postfix(
        self, session_factory
    ):
        backend, seed, latest = await self._run_revision(
            session_factory, PromptInjection(prefix=_PREFIX, postfix=_POSTFIX, first_msg="")
        )
        assert backend.system_prompts, "the tool loop never opened a session"
        tool_prompt = next(
            (p for p in backend.system_prompts if "## Revision tool contract" in p), None
        )
        assert tool_prompt is not None, "no tool-mode session was opened"
        assert _PREFIX in tool_prompt
        assert _POSTFIX in tool_prompt
        # Ordering: the user wrapper brackets the prompt file; the machine-authored tool
        # contract keeps final position, so a postfix can never override it.
        assert tool_prompt.index(_PREFIX) < tool_prompt.index(_POSTFIX)
        assert tool_prompt.index(_POSTFIX) < tool_prompt.index("## Revision tool contract")

        # The FRESH cv_adjust session in the same flow carried it too — so this asserts
        # the fresh site and the tool site together, which is the pairing that matters:
        # a wrapper present on turn 1 and gone from the revision is the exact silent
        # shape CLAUDE.md documents for a dropped structured contract.
        assert _PREFIX in seed.system_prompts[0]
        assert _POSTFIX in seed.system_prompts[0]

        # And the revision itself still worked — this is a gate on the injection, not a
        # licence for the tool loop to have quietly fallen through to rung 3.
        assert latest.version == 2
        assert self._REVISED in latest.markdown

    async def test_an_uninjected_revision_is_unchanged(self, session_factory):
        """The no-injection path must be byte-identical to the pre-injection tree — that
        is what keeps every uninjected job on the shared prompt-cache prefix."""
        backend, seed, latest = await self._run_revision(session_factory, None)
        tool_prompt = next(p for p in backend.system_prompts if "## Revision tool contract" in p)
        assert _PREFIX not in tool_prompt and _POSTFIX not in tool_prompt
        assert latest.version == 2

    async def test_first_msg_alone_leaves_the_tool_prompt_untouched(self, session_factory):
        """`first_msg` is a fresh-session USER-message field only. A revision restores a
        session and builds no initial user message, so a job carrying only a first_msg
        must produce exactly the uninjected tool prompt."""
        with_first, _s1, _d1 = await self._run_revision(
            session_factory, PromptInjection(prefix="", postfix="", first_msg=_FIRST_MSG)
        )
        without, _s2, _d2 = await self._run_revision(session_factory, None)
        a = next(p for p in with_first.system_prompts if "## Revision tool contract" in p)
        b = next(p for p in without.system_prompts if "## Revision tool contract" in p)
        assert a == b
        assert _FIRST_MSG not in a


class TestADeckAssignmentDoesNotLeakIntoARevision:
    """`feat/cv-decks` x `feat/revision-tool-use` — the third pair, and the one where the
    correct answer is "nothing happens".

    cv-decks resolves a per-job deck into `run_stage(..., cv_structure_path=...)`, which
    `fit_assessment` and `cv_adjust` read as the BASE CV. A revision reads neither: it
    builds its working copy from `_latest_document_object` — the already-tailored
    Document — and patches that. So a revision on a deck-assigned job must be byte-for-byte
    unaffected by which deck the job holds.

    Worth pinning precisely because it is the pair a reader would assume needs wiring: the
    two features share `run_stage`'s signature, and "the revision should use the job's
    deck" is a plausible-sounding change that would in fact reintroduce the whole-document
    rewrite the tool loop exists to avoid.
    """

    async def test_the_revision_patches_the_document_not_the_deck(self, tmp_path, session_factory):
        settings = Settings(db_path=tmp_path / "jsa.sqlite")
        deck = await cv_decks.create_deck(settings, name="unrelated")
        await cv_decks.save_deck(
            settings,
            deck.id,
            CVDocument.model_validate(_cv("Deck Person", "A summary that must never appear.")),
        )
        deck_path = await make_base_cv_resolver(settings)(deck.id)

        rev = TestInjectionSurvivesAToolModeRevision()
        async with session_factory() as session:
            job = await _insert_job(session, uuid4().hex[:16], state=JobState.pending,
                                    base_cv_id=deck.id)
            transition(job, JobState.running, Stage.cv_adjust)
            await session.commit()
            await run_stage(
                job,
                FakeAgentBackend([_structured_final(rev._cv_payload)]),
                Stage.cv_adjust,
                session,
                cv_structure_path=deck_path,
            )
            job = await repo.get_job(session, job.id)
            session.add(
                RevisionRequest(job_id=job.id, target=Stage.cv_adjust,
                                instruction="Tighten the summary.", origin_state="cv_review")
            )
            await session.commit()

            transition(job, JobState.running, Stage.revising_cv)
            await session.commit()
            await run_stage(
                job,
                FakeAgentBackend([
                    _tool_calls([("get_cv", {})]),
                    _tool_calls([("replace_summary", {"text": rev._REVISED})]),
                    _tool_calls([("finalize", {"change_log": "Rewrote the summary."})]),
                ]),
                Stage.revising_cv,
                session,
                cv_structure_path=deck_path,   # the deck is still resolved and passed in
            )

            docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
            latest = max(docs, key=lambda d: d.version)

        assert latest.version == 2
        assert rev._REVISED in latest.markdown          # the patch landed
        assert "Jane Doe" in latest.markdown            # the tailored document's identity
        assert "Deck Person" not in latest.markdown     # the base deck never leaked in
        assert "must never appear" not in latest.markdown


# --------------------------------------------------- 2. injection x deck, same job

class TestInjectionAndDeckOnTheSameJob:
    """`feat/prompt-injection` x `feat/cv-decks`, through a real orchestrator dispatch.

    These two features share the job row, the job DTO and the job-row UI, but land in
    different halves of the request: the deck's CV goes into the USER message, the
    injection's wrapper into the SYSTEM prompt. A job carrying both must get both — and
    the fit gate's carve-out (system yes, first_msg no) must hold with a deck present.
    """

    @pytest.fixture
    async def two_decks(self, settings):
        a = await cv_decks.create_deck(settings, name="deck-a")
        await cv_decks.save_deck(
            settings, a.id, CVDocument.model_validate(_cv("Alice Anderson", "Default deck."))
        )
        b = await cv_decks.create_deck(settings, name="deck-b")
        await cv_decks.save_deck(
            settings, b.id, CVDocument.model_validate(_cv("Bob Brown", "Assigned deck."))
        )
        return a.id, b.id

    async def _dispatch(self, settings, session_factory, *, base_cv_id, injection):
        backends: list[PromptCapturingBackend] = []

        def factory(_name: str) -> PromptCapturingBackend:
            b = PromptCapturingBackend([_final("UNFIT\nNot a match.")])
            backends.append(b)
            return b

        async with session_factory() as s:
            job = await _insert_job(
                s,
                uuid4().hex[:16],
                state=JobState.pending,
                base_cv_id=base_cv_id,
                injection=injection.model_dump_json() if injection else None,
            )
        orch = Orchestrator(
            db_session_factory=session_factory,
            backend_factory=factory,
            base_cv_resolver=make_base_cv_resolver(settings),
        )
        task = asyncio.create_task(orch.run())
        try:
            await asyncio.sleep(0.1)
            orch.kick()
            deadline = asyncio.get_event_loop().time() + 5.0
            while True:
                async with session_factory() as s:
                    row = await repo.get_job(s, job.id)
                if row.state != JobState.pending:
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    raise TimeoutError(f"job never left pending; state={row.state}")
                await asyncio.sleep(0.05)
        finally:
            orch._stopping = True
            orch.kick()
            await asyncio.wait_for(task, timeout=5.0)
        assert backends and backends[0].system_prompts, "no session was ever started"
        return backends[0]

    async def test_a_job_with_both_gets_both(self, settings, session_factory, two_decks):
        _a, b_id = two_decks
        backend = await self._dispatch(
            settings,
            session_factory,
            base_cv_id=b_id,
            injection=PromptInjection(prefix=_PREFIX, postfix=_POSTFIX, first_msg=""),
        )
        # System prompt: the user's wrapper.
        assert _PREFIX in backend.system_prompts[0]
        assert _POSTFIX in backend.system_prompts[0]
        # User message: the ASSIGNED deck, not the default one.
        assert "Bob Brown" in backend.user_msgs[0]
        assert "Alice Anderson" not in backend.user_msgs[0]

    async def test_the_fit_gate_carve_out_holds_with_a_deck_assigned(
        self, settings, session_factory, two_decks
    ):
        """Locked decision 2 of the injection plan: `fit_assessment` honors prefix and
        postfix (it is the same job) but NEVER `first_msg` — a data nudge must not be able
        to manufacture a FIT verdict. Assert it while a deck is also in play, since both
        features write into this one stage's prompts."""
        _a, b_id = two_decks
        backend = await self._dispatch(
            settings,
            session_factory,
            base_cv_id=b_id,
            injection=PromptInjection(prefix=_PREFIX, postfix=_POSTFIX, first_msg=_FIRST_MSG),
        )
        assert _PREFIX in backend.system_prompts[0]
        assert _FIRST_MSG not in backend.system_prompts[0]
        assert _FIRST_MSG not in backend.user_msgs[0]
        assert "Bob Brown" in backend.user_msgs[0]

    async def test_no_injection_and_no_deck_is_the_shared_prefix(
        self, settings, session_factory, two_decks
    ):
        """Both features off => the default deck and an unwrapped system prompt, i.e.
        exactly what a job on `main` would have seen."""
        backend = await self._dispatch(
            settings, session_factory, base_cv_id=None, injection=None
        )
        assert _PREFIX not in backend.system_prompts[0]
        assert "Alice Anderson" in backend.user_msgs[0]


# ------------------------------------------------ 3. both migrations on one old DB

class TestBothColumnsLandOnAnExistingDatabase:
    """`feat/cv-decks` x `feat/prompt-injection`, in `engine.py::init_db`.

    Each feature appends its own `try/except OperationalError` ALTER TABLE block. The
    merge conflict split each block BEFORE its own `except`, so a keep-both resolution
    produced a `try` with no handler — caught here only because it happened to be a
    syntax error. A conflict split one line either way would have dropped one ALTER
    silently instead, and NOTHING in the suite would have noticed: every other test
    builds its schema with `create_all`, which reads the model definitions and therefore
    always has both columns regardless of whether `init_db`'s ALTER path works.

    This test is the only one that exercises the upgrade path an existing ~/.jsa DB
    actually takes.
    """

    async def _columns(self, engine) -> set[str]:
        async with engine.begin() as conn:
            rows = await conn.execute(sa.text("PRAGMA table_info(jobs)"))
            return {r[1] for r in rows}

    async def test_init_db_adds_base_cv_id_and_injection_to_a_pre_feature_jobs_table(
        self, tmp_path
    ):
        url = f"sqlite+aiosqlite:///{tmp_path / 'old.sqlite'}"
        engine = create_async_engine(url)
        try:
            # Build the current schema, then take the two columns back off it to stand in
            # for a database created before either feature existed.
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(sa.text("ALTER TABLE jobs DROP COLUMN base_cv_id"))
                await conn.execute(sa.text("ALTER TABLE jobs DROP COLUMN injection"))
            before = await self._columns(engine)
            assert "base_cv_id" not in before and "injection" not in before
        finally:
            await engine.dispose()

        engine = create_async_engine(url)
        try:
            await init_db(engine)
            after = await self._columns(engine)
        finally:
            await engine.dispose()

        assert "base_cv_id" in after, "cv-decks' ALTER block was lost in the merge"
        assert "injection" in after, "prompt-injection's ALTER block was lost in the merge"

    async def test_init_db_is_idempotent_on_an_already_upgraded_database(self, tmp_path):
        """Both blocks must swallow their own OperationalError — if one lost its `except`
        the second run raises instead of being a no-op."""
        url = f"sqlite+aiosqlite:///{tmp_path / 'new.sqlite'}"
        engine = create_async_engine(url)
        try:
            await init_db(engine)
            await init_db(engine)
            after = await self._columns(engine)
        finally:
            await engine.dispose()
        assert {"base_cv_id", "injection"} <= after
