"""Byte-stability gate for the prompt-caching plan (see the plan's Phase 0).

Prompt caching is a pure prefix match: a caching provider only reports a cache hit
when the sent prefix is byte-identical to a previously-sent one. The assembled system
prompt (``assemble_system_prompt``) embeds ``json.dumps(schema, indent=2)`` where
``schema`` comes from Pydantic's ``model_json_schema()`` — nothing today asserts that
this rendering is deterministic *across process restarts*, not just within one process.
If any part of that path iterates a ``set`` (e.g. an enum/``Literal`` union materialised
through one), key/item order can vary with ``PYTHONHASHSEED``, so the same logical
schema would render as different bytes on different server restarts — silently turning
prompt caching into a pure cache-write cost with zero reads, while every other unit
test in this plan would still pass. This test is the single gate that would catch that
failure mode; run it before any other phase of the prompt-caching plan.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from jsa.db.models import Job, JobState, Stage
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.pipeline.stages import _build_fit_user_msg, _build_initial_user_msg, _get_system_prompt
from jsa.prompts.loader import read_prompt
from jsa.schema.turn_models import json_schema_for

# One prompt-file name + Stage enum member per stage that actually reaches
# assemble_system_prompt in stages.py (fit_assessment, cv_adjust, cover_letter —
# revising_cv/revising_cl reuse cv_adjust/cover_letter's prompt file, see
# _get_system_prompt, so they are not separate cases here).
_CASES = [
    ("fit_assessment", Stage.fit_assessment),
    ("cv_adjust", Stage.cv_adjust),
    ("cover_letter", Stage.cover_letter),
]

_SUBPROCESS_SNIPPET = """
import hashlib
from jsa.db.models import Stage
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.prompts.loader import read_prompt
from jsa.schema.turn_models import json_schema_for

prompt = assemble_system_prompt(
    read_prompt({prompt_name!r}),
    language="en",
    structured_model=json_schema_for(Stage.{stage_name}),
)
print(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
"""


def _assembled_prompt_hash(prompt_name: str, stage: Stage) -> str:
    prompt = assemble_system_prompt(
        read_prompt(prompt_name),
        language="en",
        structured_model=json_schema_for(stage),
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _subprocess_hash(prompt_name: str, stage: Stage, pythonhashseed: str) -> str:
    snippet = _SUBPROCESS_SNIPPET.format(prompt_name=prompt_name, stage_name=stage.name)
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        env={"PYTHONHASHSEED": pythonhashseed, **_inherited_env()},
        check=True,
        cwd=str(_repo_root()),
    )
    return result.stdout.strip()


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _inherited_env() -> dict[str, str]:
    import os

    # Preserve PATH/PYTHONPATH/venv-related vars so the subprocess can import jsa;
    # PYTHONHASHSEED itself is set explicitly by the caller and must win.
    return {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}


@pytest.mark.parametrize("prompt_name,stage", _CASES, ids=[c[0] for c in _CASES])
def test_assembled_system_prompt_is_byte_stable_within_process(prompt_name, stage):
    first = _assembled_prompt_hash(prompt_name, stage)
    second = _assembled_prompt_hash(prompt_name, stage)
    assert first == second


@pytest.mark.parametrize("prompt_name,stage", _CASES, ids=[c[0] for c in _CASES])
@pytest.mark.parametrize("pythonhashseed", ["0", "1"])
def test_assembled_system_prompt_is_byte_stable_across_processes(prompt_name, stage, pythonhashseed):
    in_process = _assembled_prompt_hash(prompt_name, stage)
    out_of_process = _subprocess_hash(prompt_name, stage, pythonhashseed)
    assert out_of_process == in_process, (
        f"assembled system prompt hash for {prompt_name!r} differs under "
        f"PYTHONHASHSEED={pythonhashseed} — prompt caching would silently never hit "
        "across server restarts; sort the non-deterministic collection at the point "
        "it is built (see plan Phase 0)"
    )


# ---------------------------------------------------------------------------
# Cross-job prefix identity: the plan's actual value claim is that every job at a
# given (stage, language, structured-mode) sends a BYTE-IDENTICAL system prefix —
# not just that one job's prefix is stable across restarts. That only holds if no
# job-derived byte (JD, company, role, CV structure, research brief) ever lands in
# the system prompt. stages.py's design keeps all of that in the *initial user
# message* (_build_initial_user_msg / _build_fit_user_msg) instead — this test
# pins that split directly, against two jobs with deliberately different content,
# rather than relying on assemble_system_prompt's signature not accepting a job.
# ---------------------------------------------------------------------------


def _make_job(**overrides) -> Job:
    data = dict(
        id="job-a",
        company="Acme Corp",
        role="Senior Widget Engineer",
        link="https://example.com/jobs/1",
        tier="A",
        jd="Build widgets at scale. Requires 5 years of widget experience.",
        jd_hash="hash0000deadbeef",
        cv_text="unused",
    )
    data.update(overrides)
    return Job(**data, state=JobState.pending)


class TestCrossJobSystemPromptIdentity:
    def test_get_system_prompt_ignores_the_job_entirely(self):
        """_get_system_prompt(stage) takes no job argument at all -- the strongest
        possible guarantee, checked here so a future signature change trips this
        test instead of silently reintroducing job-derived bytes."""
        import inspect

        sig = inspect.signature(_get_system_prompt)
        assert list(sig.parameters) == ["stage"]

    def test_two_jobs_with_different_jd_and_company_yield_identical_system_prompt(self):
        job_a = _make_job(id="job-a", company="Acme Corp", role="Widget Engineer", jd="Widgets.")
        job_b = _make_job(id="job-b", company="Globex Inc", role="Gadget Architect", jd="Gadgets, at a totally different scale, forever.")

        for stage in (Stage.cv_adjust, Stage.cover_letter):
            prompt = _get_system_prompt(stage)
            # The system prompt is a pure function of `stage` -- calling it twice
            # regardless of which job is "in scope" must be identical.
            assert prompt == _get_system_prompt(stage)

        # The job-derived content actually differs between the two jobs...
        msg_a = _build_initial_user_msg(job_a, brief=None)
        msg_b = _build_initial_user_msg(job_b, brief=None)
        assert msg_a != msg_b
        assert job_a.jd in msg_a and job_a.jd not in msg_b
        assert job_b.jd in msg_b and job_b.jd not in msg_a

        # ...and none of it ever appears in the system prompt for either stage.
        for stage, prompt_name in ((Stage.cv_adjust, "cv_adjust"), (Stage.cover_letter, "cover_letter")):
            system_prompt = read_prompt(prompt_name)
            for job in (job_a, job_b):
                assert job.jd not in system_prompt
                assert job.company not in system_prompt
                assert job.role not in system_prompt

    def test_fit_assessment_job_data_stays_out_of_the_system_prompt_too(self):
        job_a = _make_job(id="job-a", company="Acme Corp", jd="Widgets.")
        job_b = _make_job(id="job-b", company="Globex Inc", jd="Gadgets, at a totally different scale.")

        msg_a = _build_fit_user_msg(job_a, base_structure=None)
        msg_b = _build_fit_user_msg(job_b, base_structure=None)
        assert msg_a != msg_b

        system_prompt = read_prompt("fit_assessment")
        for job in (job_a, job_b):
            assert job.jd not in system_prompt
            assert job.company not in system_prompt


# ---------------------------------------------------------------------------
# Per-job prompt injection vs. the cross-job prefix invariant.
#
# The whole point of this file is that no per-job bytes reach the system prompt.
# The injection feature is the ONE deliberate exception: a job carrying a
# prefix/postfix knowingly buys itself a private cache entry. Everything below
# pins both halves of that trade so neither can drift silently — a job WITHOUT an
# injection must be bit-for-bit what it was before the feature existed, and a job
# WITH one must actually differ (a documented, intended cost, not an accident).
#
# These operate on assemble_system_prompt directly, mirroring
# TestCrossJobSystemPromptIdentity's deliberate refusal to thread a Job through a
# function that does not take one. `now` is never passed, same as every other test
# here, so the comparisons stay date-independent.
# ---------------------------------------------------------------------------


from jsa.schema.injection import PromptInjection  # noqa: E402


def _prefix_for(prompt_name: str, stage: Stage, injection=None) -> str:
    return assemble_system_prompt(
        read_prompt(prompt_name),
        language="en",
        structured_model=json_schema_for(stage),
        injection=injection,
    )


class TestInjectionAndTheCrossJobPrefix:
    @pytest.mark.parametrize("prompt_name,stage", _CASES)
    def test_two_jobs_with_no_injection_are_still_identical(self, prompt_name, stage):
        job_a = _prefix_for(prompt_name, stage, injection=None)
        job_b = _prefix_for(prompt_name, stage, injection=None)
        assert job_a == job_b

    @pytest.mark.parametrize("prompt_name,stage", _CASES)
    def test_two_all_blank_injections_are_identical_and_equal_the_no_injection_prefix(
        self, prompt_name, stage
    ):
        """normalized() collapses an all-blank triple to None, so a job whose
        injection was saved-then-cleared (or saved as whitespace) must stay inside the
        shared cache entry rather than silently owning a private one forever."""
        baseline = _prefix_for(prompt_name, stage, injection=None)
        blank_a = _prefix_for(prompt_name, stage, injection=PromptInjection())
        blank_b = _prefix_for(
            prompt_name,
            stage,
            injection=PromptInjection(prefix="   ", postfix="\n\t ", first_msg="  \n"),
        )
        assert blank_a == blank_b == baseline

    @pytest.mark.parametrize("prompt_name,stage", _CASES)
    def test_a_first_msg_only_injection_also_stays_on_the_shared_prefix(
        self, prompt_name, stage
    ):
        """first_msg rides the initial USER message, never the system prompt — so it
        must not cost the job its shared cache entry."""
        baseline = _prefix_for(prompt_name, stage, injection=None)
        assert (
            _prefix_for(prompt_name, stage, injection=PromptInjection(first_msg="do X"))
            == baseline
        )

    @pytest.mark.parametrize("prompt_name,stage", _CASES)
    def test_a_real_injection_deliberately_differs(self, prompt_name, stage):
        """Asserted, not merely tolerated: a prefix/postfix job leaves the shared
        cross-job prefix by design (the user asked for different instructions), and
        that trade must be visible here rather than discovered as a cache-miss
        mystery later. If this ever starts passing by accident — i.e. the injection
        stops reaching the system prompt — that is the regression."""
        baseline = _prefix_for(prompt_name, stage, injection=None)
        for injection in (
            PromptInjection(prefix="LEAD WITH PAYMENTS"),
            PromptInjection(postfix="KEEP IT TO ONE PAGE"),
            PromptInjection(prefix="BEFORE", postfix="AFTER"),
        ):
            assert _prefix_for(prompt_name, stage, injection=injection) != baseline

    @pytest.mark.parametrize("prompt_name,stage", _CASES)
    def test_the_same_injection_is_still_deterministic(self, prompt_name, stage):
        """Two jobs sharing an identical injection still share a prefix — the feature
        costs a cache partition, not cache determinism."""
        inj = PromptInjection(prefix="BEFORE", postfix="AFTER")
        assert _prefix_for(prompt_name, stage, injection=inj) == _prefix_for(
            prompt_name, stage, injection=PromptInjection(prefix="BEFORE", postfix="AFTER")
        )
