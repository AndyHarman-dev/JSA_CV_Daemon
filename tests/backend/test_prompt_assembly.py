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

from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.schema.turn_models import json_schema_for
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
