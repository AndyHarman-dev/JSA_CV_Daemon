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
