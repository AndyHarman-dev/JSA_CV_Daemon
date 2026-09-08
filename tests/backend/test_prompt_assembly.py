"""Tests for jsa.pipeline.prompt_assembly.assemble_system_prompt.

Two concerns:
1. PARITY GATE (CLAUDE.md → "Parity gate for replace / delete refactors"): the
   sentinel-mode path (structured_model=None) must be byte-identical, for every
   language in the catalog, to the pre-refactor `stages.py::_with_language_directive`
   — captured as a golden fixture BEFORE that function was deleted (see
   tests/backend/fixtures/language_directive_golden.json's own header comment in this
   file). This is the only thing standing between "CLI sentinel-mode behavior is
   unchanged" and a silent prompt-wording drift for claude-cli/google-cli.
2. Structured-mode composition: the appended contract section + language-directive
   variant, for both fit and non-fit stages.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from jsa.agents.tool_spec import tools_for
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.schema.turn_models import json_schema_for, json_schema_for_infer
from jsa.db.models import Stage

_GOLDEN_PATH = Path(__file__).parent / "fixtures" / "language_directive_golden.json"


def _load_golden() -> list[dict]:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


class TestSentinelModeParityGate:
    """assemble_system_prompt(structured_model=None) must match the golden fixture,
    captured from the original stages.py::_with_language_directive before it moved."""

    @pytest.mark.parametrize(
        "case",
        _load_golden(),
        ids=lambda c: f"{c['prompt_label']}-{c['language_code']}-fit={c['fit_verdict']}",
    )
    def test_matches_golden_output(self, case):
        result = assemble_system_prompt(
            case["prompt_text"],
            language=case["language_code"],
            structured_model=None,
            fit_verdict=case["fit_verdict"],
        )
        assert result == case["output"]

    def test_golden_fixture_is_non_trivial(self):
        # Guard against an accidentally-empty/truncated fixture silently passing everything.
        golden = _load_golden()
        assert len(golden) == 84
        assert any("Spanish" in c["output"] for c in golden)
        assert any(c["output"] == c["prompt_text"] for c in golden)  # "en" no-op cases


class TestDocumentOnlyContract:
    """The third contract shape (`jsa/pipeline/infer_structure.py`'s job-less call):
    a one-shot `kind: "final"` document turn with no question branch, and no language
    directive at all."""

    def test_document_shape_replaces_the_question_or_final_union(self):
        schema = json_schema_for_infer()
        result = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=schema, document_only=True
        )
        assert result.startswith("BASE PROMPT")
        assert "## Structured output contract" in result
        assert "no question branch" in result.lower()
        assert json.dumps(schema, indent=2) in result
        # None of the turn-union's question machinery leaks in.
        assert "suggested_replies" not in result
        assert '`"question"`' not in result

    def test_states_that_schema_minimums_are_not_a_target(self):
        """Confirmed live (2026-09-07, gemini-3.5-flash): a structured inference reply
        came back schema-valid with a Summary section and nothing else — no Experience,
        no Skills. `sections` only has `minItems: 1` and no field carries a description,
        so NOTHING in the schema asks for completeness, and the contract is the last
        thing the model reads before answering.

        Same failure class as the `question`-must-be-self-contained clause in the turn
        contract (see CLAUDE.md): schema-valid, semantically empty, unfixable by the
        schema itself. Do not drop this as redundant verbosity — it is the fix."""
        result = assemble_system_prompt(
            "BASE PROMPT",
            language="en",
            structured_model=json_schema_for_infer(),
            document_only=True,
        )
        assert "never the AMOUNT" in result
        assert "minimums are not a target" in result
        assert "Transcribe EVERY section" in result

    def test_suppresses_the_language_directive_outright(self):
        """Not merely because `language == "en"` short-circuits it — the point of the
        flag is that a non-English preference is ignored on this path too."""
        schema = json_schema_for_infer()
        for language in ("en", "fr", "de"):
            result = assemble_system_prompt(
                "BASE PROMPT", language=language, structured_model=schema, document_only=True
            )
            assert "## Output language" not in result

    def test_inert_in_sentinel_mode(self):
        """`run_infer` passes `document_only=True` unconditionally, alongside a schema
        that is None on a CLI backend — that combination must be a no-op."""
        assert assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=None, document_only=True
        ) == "BASE PROMPT"

    def test_mutually_exclusive_with_fit_verdict(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            assemble_system_prompt(
                "BASE PROMPT",
                language="en",
                structured_model=json_schema_for_infer(),
                document_only=True,
                fit_verdict=True,
            )


class TestStructuredModeComposition:
    def test_appends_contract_and_no_language_directive_for_english(self):
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert result.startswith("BASE PROMPT")
        assert "## Structured output contract" in result
        assert "## Output language" not in result  # english → no directive needed
        assert json.dumps(schema, indent=2) in result

    def test_appends_language_directive_variant_for_non_english(self):
        schema = json_schema_for(Stage.cover_letter)
        result = assemble_system_prompt("BASE PROMPT", language="fr", structured_model=schema)
        assert "## Structured output contract" in result
        assert "## Output language" in result
        assert "French (fr)" in result
        # Structured variant, not the sentinel one:
        assert "sentinel-block instructions elsewhere in this prompt" in result
        assert "must stay exactly as spelled, in English/ASCII" not in result

    def test_precedence_and_no_sentinel_marker_lines_present(self):
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert "supersedes any" in result
        assert "<<<NEED_INPUT>>>" in result  # named explicitly in the precedence line
        assert "corrupt the payload" in result

    def test_kind_semantics_present_for_non_fit_stage(self):
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert '`kind`' in result
        assert '"question"' in result
        assert '"final"' in result

    def test_fit_verdict_shape_used_for_fit_stage(self):
        schema = json_schema_for(Stage.fit_assessment)
        result = assemble_system_prompt(
            "BASE PROMPT", language="es", structured_model=schema, fit_verdict=True
        )
        assert "`verdict`" in result
        assert "`reason`" in result
        assert "Spanish (es)" in result
        assert "must stay in English; only `reason`" in result

    def test_structured_schema_embedded_verbatim(self):
        schema = json_schema_for(Stage.revising_cl)
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert json.dumps(schema, indent=2) in result

    def test_question_field_self_containment_instruction_present_for_non_fit(self):
        """Regression for the parked-with-no-strategy bug: opencode-zen/opencode-go
        models (e.g. nemotron-3.5-lightning-free) returned a bare confirmation
        question — {"kind":"question","question":"Shall I proceed...","payload":null}
        — with the Phase-1 written strategy nowhere in the reply, because the schema
        gives `kind: "question"` turns nowhere else to put it and the contract never
        said `question` must be self-contained. Confirmed live against two parked
        cv_adjust jobs (jsa.sqlite, both backend_name=opencode-zen)."""
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert "ONLY field the user will see on a question turn" in result

    def test_question_field_self_containment_instruction_absent_for_fit(self):
        schema = json_schema_for(Stage.fit_assessment)
        result = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=schema, fit_verdict=True
        )
        assert "ONLY field the user will see on a question turn" not in result


class TestForResume:
    """Regression coverage for the stuck-job bug: structured-capable backends are
    wire-stateless and resend the system prompt on every restore_session/send_message
    call, so the structured contract must be re-appended on resume or the model loses
    the kind/question/payload explanation and can loop forever re-asking its opening
    question (confirmed live against a cv_adjust job on opencode-go/longcat-2.0)."""

    def test_structured_resume_still_gets_contract(self):
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=schema, for_resume=True
        )
        assert "## Structured output contract" in result
        assert json.dumps(schema, indent=2) in result

    def test_structured_resume_never_gets_language_directive(self):
        # Unchanged invariant (CLAUDE.md → "Language preference"): a resumed session
        # never gets the language directive re-injected, structured mode included.
        schema = json_schema_for(Stage.cover_letter)
        result = assemble_system_prompt(
            "BASE PROMPT", language="fr", structured_model=schema, for_resume=True
        )
        assert "## Structured output contract" in result
        assert "## Output language" not in result

    def test_sentinel_resume_is_unchanged_bare_prompt(self):
        # CLI-backend behavior (structured_model=None) must stay exactly what every
        # restore_session call site already relied on: the bare prompt, untouched.
        result = assemble_system_prompt(
            "BASE PROMPT", language="fr", structured_model=None, for_resume=True
        )
        assert result == "BASE PROMPT"


class TestCurrentDateDirective:
    """The date models see is trained-cutoff data, not live data — without this
    directive, models judge a CV's own (correct) recent/current dates as errors.
    now=None (the default) must stay a strict no-op: the golden-fixture parity gate
    above calls assemble_system_prompt with no `now` at all and must keep passing."""

    _NOW = datetime(2026, 9, 3, 14, 30, 0)

    def test_now_none_is_a_strict_no_op_sentinel_fresh(self):
        result = assemble_system_prompt("BASE PROMPT", language="en", structured_model=None)
        assert result == "BASE PROMPT"

    def test_now_none_is_a_strict_no_op_structured_fresh(self):
        schema = json_schema_for(Stage.cv_adjust)
        with_date = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=schema, now=self._NOW
        )
        without_date = assemble_system_prompt("BASE PROMPT", language="en", structured_model=schema)
        assert "## Current date" not in without_date
        assert "## Current date" in with_date

    def test_sentinel_fresh_gets_date_directive_with_day_granularity_only(self):
        result = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=None, now=self._NOW
        )
        assert "## Current date" in result
        assert "2026-09-03" in result
        assert "14:30" not in result  # day granularity only, never clock time

    def test_sentinel_fresh_non_english_still_gets_date_directive(self):
        result = assemble_system_prompt(
            "BASE PROMPT", language="fr", structured_model=None, now=self._NOW
        )
        assert "## Output language" in result
        assert "## Current date" in result
        # Appended last, after the language directive.
        assert result.index("## Output language") < result.index("## Current date")

    def test_structured_fresh_gets_date_directive_after_contract_and_language(self):
        schema = json_schema_for(Stage.cover_letter)
        result = assemble_system_prompt(
            "BASE PROMPT", language="fr", structured_model=schema, now=self._NOW
        )
        assert "## Current date" in result
        assert result.index("## Structured output contract") < result.index("## Current date")
        assert result.index("## Output language") < result.index("## Current date")

    def test_structured_resume_gets_date_directive(self):
        # Wire-stateless backends resend the system prompt every call, so a job
        # parked overnight in awaiting_input must resume with today's date, not the
        # one at job launch.
        schema = json_schema_for(Stage.cv_adjust)
        result = assemble_system_prompt(
            "BASE PROMPT",
            language="en",
            structured_model=schema,
            for_resume=True,
            now=self._NOW,
        )
        assert "## Current date" in result
        assert "2026-09-03" in result

    def test_sentinel_resume_never_gets_date_directive(self):
        # Real CLI session already carries the date from its fresh start; nothing
        # needs to change on resume, same as the language directive.
        result = assemble_system_prompt(
            "BASE PROMPT",
            language="en",
            structured_model=None,
            for_resume=True,
            now=self._NOW,
        )
        assert result == "BASE PROMPT"

    def test_instructs_model_not_to_flag_future_relative_dates_as_errors(self):
        result = assemble_system_prompt(
            "BASE PROMPT", language="en", structured_model=None, now=self._NOW
        )
        assert "not an error" in result
        assert "training" in result


class TestToolContract:
    """revision-tool-use plan, Phase 4 — `_tool_contract` + the `tool_model=` kwarg.

    Both rungs get a contract (the plan is explicit that this is NOT "native = wire only,
    prompt = prompt only"): a provider enforces the SHAPE of a call but says nothing about
    when to call `get_cv` versus `finalize`, or that a question must precede any edit (D8).
    """

    @staticmethod
    def _specs(stage: Stage = Stage.revising_cv):
        return tools_for(stage)

    def test_no_tool_model_is_byte_identical_to_before(self):
        """The golden-parity path. `structured_model=None, tool_model=None` must be
        untouched by this feature."""
        for lang in ("en", "fr", "de"):
            assert assemble_system_prompt(
                "BASE", language=lang
            ) == assemble_system_prompt("BASE", language=lang, tool_model=None)

    def test_both_rungs_carry_the_semantic_contract(self):
        for native in (True, False):
            out = assemble_system_prompt(
                "BASE", language="en", tool_model=self._specs(), native_tools=native
            )
            assert "## Revision tool contract" in out
            assert "get_cv" in out and "finalize" in out and "ask_user" in out
            # D8: ask_user discards mutations, so questions must precede edits.
            assert "DISCARDS" in out
            # D1's budget.
            assert "10 tool calls" in out
            # The id discipline.
            assert "never invent one" in out

    def test_all_six_error_codes_are_documented_in_both_rungs(self):
        for native in (True, False):
            out = assemble_system_prompt(
                "BASE", language="en", tool_model=self._specs(), native_tools=native
            )
            for code in (
                "unknown_id", "stale_id", "bad_argument",
                "validation_failed", "budget_exhausted", "not_executed",
            ):
                assert code in out, f"{code} missing from native={native} contract"

    def test_native_rung_omits_the_grammar_and_the_inlined_schemas(self):
        """Schemas ride the wire in `tools` on the native rung — inlining them there
        would duplicate the provider's own enforcement for no benefit."""
        out = assemble_system_prompt(
            "BASE", language="en", tool_model=self._specs(), native_tools=True
        )
        assert "<<<TOOL_CALLS>>>" not in out
        assert "Tool schemas" not in out

    def test_prompt_rung_carries_the_grammar_and_every_schema(self):
        specs = self._specs()
        out = assemble_system_prompt(
            "BASE", language="en", tool_model=specs, native_tools=False
        )
        assert "<<<TOOL_CALLS>>>" in out
        assert "### Tool schemas" in out
        for spec in specs:
            assert f"#### {spec.name}" in out

    def test_prompt_rung_supersedes_the_sentinel_instructions(self):
        out = assemble_system_prompt(
            "BASE", language="en", tool_model=self._specs(), native_tools=False
        )
        assert "SUPERSEDES" in out

    def test_cl_stage_gets_the_letter_vocabulary_not_the_cv_one(self):
        out = assemble_system_prompt(
            "BASE", language="en", tool_model=self._specs(Stage.revising_cl)
        )
        assert "get_letter" in out and "replace_paragraph" in out
        assert "replace_summary" not in out

    def test_structured_and_tool_model_together_raises(self):
        """Phase 3: tool mode and structured mode are mutually exclusive per request —
        the terminal tool's arguments ARE the structured output."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            assemble_system_prompt(
                "BASE",
                language="en",
                structured_model={"type": "object"},
                tool_model=self._specs(),
            )

    def test_language_and_for_resume_do_not_alter_the_tool_contract(self):
        """run_tool_loop only ever restores (revisions never start_session), so the tool
        path is always the for_resume shape and a resumed session already committed to
        its language."""
        base = assemble_system_prompt("BASE", language="en", tool_model=self._specs())
        for lang in ("fr", "de"):
            for resume in (True, False):
                assert assemble_system_prompt(
                    "BASE", language=lang, tool_model=self._specs(), for_resume=resume
                ) == base


class TestToolContractCarriesTheInjection:
    """Cross-feature gate: `feat/revision-tool-use` x `feat/prompt-injection`.

    These two features were built on separate branches and merged cleanly into
    `assemble_system_prompt` WITHOUT a conflict on the tool branch's return statement.
    prompt-injection computes `base` (prefix + prompt_text + postfix) at the top of the
    function and rewrites every branch to compose from it; revision-tool-use adds a new
    FIRST branch that returned `prompt_text`. Taking both verbatim compiles, keeps every
    test on both branches green, and silently drops the user's per-job wrapper from every
    tool-mode revision — on the prompt rung, that system prompt is the ONLY transport the
    contract and the wrapper have.

    Neither branch could have owned this test: neither one's tree contains both features.
    """

    @staticmethod
    def _specs(stage: Stage = Stage.revising_cv):
        return tools_for(stage)

    @staticmethod
    def _inj(**kw):
        from jsa.schema.injection import PromptInjection

        return PromptInjection(**{"prefix": "", "postfix": "", "first_msg": "", **kw})

    @pytest.mark.parametrize("native", [True, False])
    def test_prefix_and_postfix_bracket_the_prompt_on_both_rungs(self, native):
        out = assemble_system_prompt(
            "BASE PROMPT",
            language="en",
            tool_model=self._specs(),
            native_tools=native,
            injection=self._inj(prefix="ALWAYS ANSWER IN THE PAST TENSE.", postfix="NEVER HEDGE."),
        )
        assert "ALWAYS ANSWER IN THE PAST TENSE." in out
        assert "NEVER HEDGE." in out
        # Order: prefix, then the prompt file, then postfix, then the machine-authored
        # contract — the same precedence rule the structured path is held to, because a
        # postfix landing after the contract becomes the last word over it.
        assert (
            out.index("ALWAYS ANSWER IN THE PAST TENSE.")
            < out.index("BASE PROMPT")
            < out.index("NEVER HEDGE.")
            < out.index("## Revision tool contract")
        )

    @pytest.mark.parametrize("native", [True, False])
    def test_no_injection_is_byte_identical_to_no_injection_kwarg(self, native):
        """The uninjected path must not shift by a single byte — that is what keeps the
        cross-job prompt-cache prefix shared for every job without an injection."""
        specs = self._specs()
        bare = assemble_system_prompt("BASE", language="en", tool_model=specs, native_tools=native)
        for injection in (None, self._inj(), self._inj(prefix="   ", postfix="\n\t")):
            assert (
                assemble_system_prompt(
                    "BASE",
                    language="en",
                    tool_model=specs,
                    native_tools=native,
                    injection=injection,
                )
                == bare
            )

    def test_the_cover_letter_stage_vocabulary_carries_it_too(self):
        out = assemble_system_prompt(
            "BASE",
            language="en",
            tool_model=self._specs(Stage.revising_cl),
            injection=self._inj(prefix="PFX", postfix="SFX"),
        )
        assert "get_letter" in out
        assert out.index("PFX") < out.index("BASE") < out.index("SFX")

    def test_first_msg_alone_never_touches_the_system_prompt(self):
        """`first_msg` is a USER-message field (`_build_initial_user_msg`), never a system
        one — and the fit gate never gets it at all (locked decision 2). A job carrying
        only a first_msg must produce the byte-identical shared system prefix."""
        specs = self._specs()
        assert assemble_system_prompt(
            "BASE", language="en", tool_model=specs,
            injection=self._inj(first_msg="mention 6 years of Rust"),
        ) == assemble_system_prompt("BASE", language="en", tool_model=specs)
