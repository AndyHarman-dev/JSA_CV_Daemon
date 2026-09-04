"""Revision-tool-use plan — the patch/rewrite parity gate.

A **permanent** regression gate, not a migration check: the full-document rewrite
is not going away, it lives forever as the ladder's rung 3 (the fallback whenever
a backend can't reach a tool rung, or the model won't use one). So the patched
path and the rewrite path must keep agreeing on observable output indefinitely.

The claim, and why it isn't a tautology: a tool-patched revision reaches
``_handle_final`` as an ordinary ``kind="final"`` reply that ``run_tool_loop``
synthesized from ``CvWorkingCopy.finalize()``/``ClWorkingCopy.finalize()``, and
from there runs through the *same* ``_validate_final_content`` gate, the *same*
``cv_to_markdown``/``cover_letter_to_markdown`` call, and the *same* checkpoint as
a model-emitted full rewrite. Nothing about that is guaranteed by construction —
the working copy rebuilds the document from plain dicts with minted ids stripped,
so a dropped field, a lost default, or a reordered section would show up here and
nowhere else.

Deliberately **not** asserted (per the plan): that a patched revision and a
full-rewrite revision produce the same *prose*. Two runs of a generative step
differ; that diff never closes and asserting it would be permanently red. The two
lanes are therefore scripted to carry the same LOGICAL payload — a patch whose
result equals the document the rewrite lane emits — and only the observable
outputs are compared.

Also deliberately not asserted: raw ``Message`` text. Same exemption as
``test_mode_parity.py`` — the tool lane persists a ``role="user"`` instruction row,
``role="tool"`` execution-log rows, and a ``role="assistant"`` row holding
``finalize``'s canonical JSON, where the rewrite lane persists the model's own
sentinel-wrapped text. Different by construction, same logical outcome.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.agents.base import AgentBackend, AgentReply, ToolCall
from jsa.db import repo
from jsa.db.models import Base, Job, JobState, RevisionRequest, Stage
from jsa.pipeline.stages import _tools_for, run_stage
from jsa.pipeline.state_machine import transition
from tests.backend.fakes.fake_backend import FakeAgentBackend
from tests.backend.fakes.finals import cl_final, cv_final, tool_loop_miss


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
async def session_pair(session_factory):
    """Two independent sessions over separate in-memory DBs — one per lane — so the
    patched run and the rewrite run can't interfere with each other. Same fixture
    shape as test_mode_parity.py's."""
    async with session_factory() as a, session_factory() as b:
        yield a, b


async def _insert_job(session: AsyncSession, job_id: str) -> Job:
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


async def _insert_revision_request(
    session: AsyncSession, job_id: str, target: Stage, instruction: str, origin_state: str
) -> None:
    session.add(
        RevisionRequest(
            job_id=job_id,
            target=target,
            instruction=instruction,
            origin_state=origin_state,
        )
    )
    await session.commit()


def _tool_calls(calls: list[tuple[str, dict]]) -> AgentReply:
    """A tool_calls reply in the shape a backend hands to run_tool_loop."""
    return AgentReply(
        raw="tool_calls",
        content="tool_calls",
        kind="tool_calls",
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(calls)
        ],
    )


# The seed CV both lanes start from, and the one edit the revision makes.
_ORIGINAL_SUMMARY = "Senior engineer with eight years of experience across payments."
_REVISED_SUMMARY = "Senior payments engineer; eight years shipping high-throughput systems."

_CV_PAYLOAD = {
    "contact": {"name": "Jane Doe", "email": "jane.doe@example.com", "phone": "+1-555-867-5309"},
    "sections": [
        {"name": "Summary", "text": _ORIGINAL_SUMMARY},
        {
            "name": "Experience",
            "entries": [
                {
                    "heading": "Senior Engineer",
                    "subheading": "Acme",
                    "dates": "2019-present",
                    "bullets": ["Built a distributed payment pipeline", "Led a service migration"],
                },
            ],
        },
    ],
}

# What the REWRITE lane's model emits: the same document with the summary swapped.
# The PATCH lane reaches this by calling replace_summary instead.
_CV_REVISED_PAYLOAD = {
    **_CV_PAYLOAD,
    "sections": [
        {"name": "Summary", "text": _REVISED_SUMMARY},
        _CV_PAYLOAD["sections"][1],
    ],
}

_ORIGINAL_CL_OPENING = (
    "I am excited to apply because your payments mission lines up with my eight "
    "years of building high-throughput transaction systems."
)
_REVISED_CL_OPENING = (
    "Your payments mission is exactly where I want to be: I have spent eight years "
    "building high-throughput transaction systems and would bring that directly."
)
_CL_CLOSING = "I am confident my background aligns well with what your team needs."

_CL_PAYLOAD = {
    "salutation": "Dear Hiring Manager,",
    "paragraphs": [_ORIGINAL_CL_OPENING, _CL_CLOSING],
    "signoff": "Sincerely,\nCandidate Name",
}

_CL_REVISED_PAYLOAD = {
    **_CL_PAYLOAD,
    "paragraphs": [_REVISED_CL_OPENING, _CL_CLOSING],
}


def _structured_final(payload: dict) -> AgentReply:
    content = json.dumps(payload)
    return AgentReply(raw=f"<<<FINAL>>>\n{content}\n<<<END>>>", content=content, kind="final")


async def _seed_cv_document(session: AsyncSession, job: Job) -> Job:
    """Run a real cv_adjust so the job has a Document with a populated `.structured`
    column — the working-copy seed `_latest_document_object` reads. A hand-inserted
    Document row would skip the tool loop entirely, which is exactly what this gate
    must not do."""
    transition(job, JobState.running, Stage.cv_adjust)
    await session.commit()
    await run_stage(job, FakeAgentBackend([_structured_final(_CV_PAYLOAD)]), Stage.cv_adjust, session)
    return await repo.get_job(session, job.id)


async def _seed_cl_document(session: AsyncSession, job: Job) -> Job:
    """cv_adjust, approve the CV gate, then cover_letter — the shortest real path to a
    cover-letter Document with a populated `.structured` column."""
    job = await _seed_cv_document(session, job)
    transition(job, JobState.cv_done)
    await session.commit()
    transition(job, JobState.running, Stage.cover_letter)
    await session.commit()
    await run_stage(job, FakeAgentBackend([_structured_final(_CL_PAYLOAD)]), Stage.cover_letter, session)
    return await repo.get_job(session, job.id)


class TestCvRevisionParity:
    """A patched revising_cv and a rewritten revising_cv produce identical output."""

    async def test_patched_and_rewritten_cv_revisions_are_identical(self, session_pair):
        patch_session, rewrite_session = session_pair
        patch_job = await _insert_job(patch_session, "parity-patch-cv")
        rewrite_job = await _insert_job(rewrite_session, "parity-rewrite-cv")

        patch_job = await _seed_cv_document(patch_session, patch_job)
        rewrite_job = await _seed_cv_document(rewrite_session, rewrite_job)
        assert patch_job.state == rewrite_job.state == JobState.cv_review

        instruction = "Tighten the summary and lead with payments."
        await _insert_revision_request(
            patch_session, patch_job.id, Stage.cv_adjust, instruction, "cv_review"
        )
        await _insert_revision_request(
            rewrite_session, rewrite_job.id, Stage.cv_adjust, instruction, "cv_review"
        )

        # PATCH lane: the model reads the CV, replaces the summary, finalizes. The
        # loop enters at the prompt rung (FakeAgentBackend leaves
        # supports_native_tools False) and never falls through to rung 3.
        patch_backend = FakeAgentBackend([
            _tool_calls([("get_cv", {})]),
            _tool_calls([("replace_summary", {"text": _REVISED_SUMMARY})]),
            _tool_calls([("finalize", {"change_log": "Rewrote the summary."})]),
        ])

        # REWRITE lane: the model ignores the tool contract, so the loop gives up
        # (tool_loop_miss) and rung 3 emits the whole revised document.
        rewrite_backend = FakeAgentBackend([
            tool_loop_miss(),
            _structured_final(_CV_REVISED_PAYLOAD),
        ])

        transition(patch_job, JobState.running, Stage.revising_cv)
        await patch_session.commit()
        transition(rewrite_job, JobState.running, Stage.revising_cv)
        await rewrite_session.commit()

        await run_stage(patch_job, patch_backend, Stage.revising_cv, patch_session)
        await run_stage(rewrite_job, rewrite_backend, Stage.revising_cv, rewrite_session)

        patch_docs = await repo.get_documents(patch_session, patch_job.id, stage=Stage.cv_adjust)
        rewrite_docs = await repo.get_documents(rewrite_session, rewrite_job.id, stage=Stage.cv_adjust)
        patch_latest = max(patch_docs, key=lambda d: d.version)
        rewrite_latest = max(rewrite_docs, key=lambda d: d.version)

        # Both lanes produced a v2 — proof the patch lane actually wrote a document
        # rather than, say, parking or silently no-opping.
        assert patch_latest.version == rewrite_latest.version == 2

        # markdown: byte-identical. This is the strongest of the three — it goes
        # through the same cv_to_markdown(structured_obj) call in _handle_final.
        assert patch_latest.markdown == rewrite_latest.markdown

        # structured: compared as PARSED JSON, not as strings. Key order legitimately
        # differs between a patched-and-redumped document and a model-emitted one.
        assert json.loads(patch_latest.structured) == json.loads(rewrite_latest.structured)

        # The revision actually took: the new summary is in, the old one is out.
        assert _REVISED_SUMMARY in patch_latest.markdown
        assert _ORIGINAL_SUMMARY not in patch_latest.markdown

        refreshed_patch = await repo.get_job(patch_session, patch_job.id)
        refreshed_rewrite = await repo.get_job(rewrite_session, rewrite_job.id)
        assert refreshed_patch.state == refreshed_rewrite.state == JobState.cv_review
        assert refreshed_patch.current_stage == refreshed_rewrite.current_stage


class TestClRevisionParity:
    """The same gate for revising_cl — a different working copy and tool vocabulary."""

    async def test_patched_and_rewritten_cl_revisions_are_identical(self, session_pair):
        patch_session, rewrite_session = session_pair
        patch_job = await _insert_job(patch_session, "parity-patch-cl")
        rewrite_job = await _insert_job(rewrite_session, "parity-rewrite-cl")

        patch_job = await _seed_cl_document(patch_session, patch_job)
        rewrite_job = await _seed_cl_document(rewrite_session, rewrite_job)
        assert patch_job.state == rewrite_job.state == JobState.review

        instruction = "Make the opening paragraph more direct."
        await _insert_revision_request(
            patch_session, patch_job.id, Stage.cover_letter, instruction, "review"
        )
        await _insert_revision_request(
            rewrite_session, rewrite_job.id, Stage.cover_letter, instruction, "review"
        )

        # get_letter mints paragraph ids p1, p2 in order — replace_paragraph targets
        # the first one. The id discipline is exercised end to end here: a wrong id
        # would come back as unknown_id and the finalize would carry the OLD text.
        patch_backend = FakeAgentBackend([
            _tool_calls([("get_letter", {})]),
            _tool_calls([("replace_paragraph", {"paragraph_id": "p1", "text": _REVISED_CL_OPENING})]),
            _tool_calls([("finalize", {"change_log": "Rewrote the opening."})]),
        ])
        rewrite_backend = FakeAgentBackend([
            tool_loop_miss(),
            _structured_final(_CL_REVISED_PAYLOAD),
        ])

        transition(patch_job, JobState.running, Stage.revising_cl)
        await patch_session.commit()
        transition(rewrite_job, JobState.running, Stage.revising_cl)
        await rewrite_session.commit()

        await run_stage(patch_job, patch_backend, Stage.revising_cl, patch_session)
        await run_stage(rewrite_job, rewrite_backend, Stage.revising_cl, rewrite_session)

        patch_docs = await repo.get_documents(patch_session, patch_job.id, stage=Stage.cover_letter)
        rewrite_docs = await repo.get_documents(rewrite_session, rewrite_job.id, stage=Stage.cover_letter)
        patch_latest = max(patch_docs, key=lambda d: d.version)
        rewrite_latest = max(rewrite_docs, key=lambda d: d.version)

        assert patch_latest.version == rewrite_latest.version == 2
        assert patch_latest.markdown == rewrite_latest.markdown
        assert json.loads(patch_latest.structured) == json.loads(rewrite_latest.structured)
        assert _REVISED_CL_OPENING in patch_latest.markdown
        assert _ORIGINAL_CL_OPENING not in patch_latest.markdown

        refreshed_patch = await repo.get_job(patch_session, patch_job.id)
        refreshed_rewrite = await repo.get_job(rewrite_session, rewrite_job.id)
        assert refreshed_patch.state == refreshed_rewrite.state == JobState.review
        assert refreshed_patch.current_stage == refreshed_rewrite.current_stage


class TestUnpatchedRegionsAreUntouched:
    """The reason this gate exists at all: silent drift in a region the patch never
    addressed. A dropped contact field or a lost Experience bullet would leave both
    lanes' markdown *equal to each other* only if the rewrite lane dropped it too —
    so pin the untouched regions against the ORIGINAL seed as well."""

    async def test_patch_preserves_every_region_it_did_not_address(self, session_pair):
        patch_session, _ = session_pair
        job = await _insert_job(patch_session, "parity-untouched")
        job = await _seed_cv_document(patch_session, job)

        seed_docs = await repo.get_documents(patch_session, job.id, stage=Stage.cv_adjust)
        seed_structured = json.loads(max(seed_docs, key=lambda d: d.version).structured)

        await _insert_revision_request(
            patch_session, job.id, Stage.cv_adjust, "Tighten the summary.", "cv_review"
        )
        backend = FakeAgentBackend([
            _tool_calls([("get_cv", {})]),
            _tool_calls([("replace_summary", {"text": _REVISED_SUMMARY})]),
            _tool_calls([("finalize", {"change_log": "Rewrote the summary."})]),
        ])
        transition(job, JobState.running, Stage.revising_cv)
        await patch_session.commit()
        await run_stage(job, backend, Stage.revising_cv, patch_session)

        docs = await repo.get_documents(patch_session, job.id, stage=Stage.cv_adjust)
        patched = json.loads(max(docs, key=lambda d: d.version).structured)

        assert patched["contact"] == seed_structured["contact"]
        seed_experience = [s for s in seed_structured["sections"] if s["name"] == "Experience"]
        patched_experience = [s for s in patched["sections"] if s["name"] == "Experience"]
        assert patched_experience == seed_experience
        assert [s["name"] for s in patched["sections"]] == [
            s["name"] for s in seed_structured["sections"]
        ]


class TestToolsAreStageScoped:
    """D3's mechanical enforcement, asserted at the _tools_for level: no stage other
    than revising_cv/revising_cl is ever offered tools, whatever the backend can do."""

    @pytest.mark.parametrize(
        "stage", [Stage.fit_assessment, Stage.cv_adjust, Stage.cover_letter]
    )
    def test_non_revision_stages_never_get_tools(self, stage):
        for backend in (
            FakeAgentBackend([], supports_native_tools=True),
            FakeAgentBackend([], supports_native_tools=False),
        ):
            assert _tools_for(backend, stage) is None

    @pytest.mark.parametrize("stage", [Stage.revising_cv, Stage.revising_cl])
    def test_revision_stages_get_tools_on_a_reachable_backend(self, stage):
        assert _tools_for(FakeAgentBackend([], supports_native_tools=True), stage) is not None
        # Non-native but restore_applies_system_prompt (the default) → prompt rung.
        assert _tools_for(FakeAgentBackend([], supports_native_tools=False), stage) is not None

    @pytest.mark.parametrize("stage", [Stage.revising_cv, Stage.revising_cl])
    def test_no_tools_when_neither_rung_is_reachable(self, stage):
        """claude-cli/google-cli's shape: restore_session resumes a provider-held
        conversation by id and discards the system prompt, so the prompt rung's tool
        contract can never reach the model — and there is no native channel either.
        Such a backend must skip the loop entirely rather than burn a turn on it."""

        class _ProviderHeldSessionBackend(FakeAgentBackend):
            restore_applies_system_prompt = False

        backend = _ProviderHeldSessionBackend([], supports_native_tools=False)
        assert _tools_for(backend, stage) is None

    def test_the_flag_defaults_true_on_the_abstract_backend(self):
        """A new backend that says nothing keeps the prompt rung — only a backend that
        explicitly opts out (because its session lives on the provider's side) loses it."""
        assert AgentBackend.restore_applies_system_prompt is True
