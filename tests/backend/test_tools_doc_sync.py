"""docs/TOOLS.md must not drift from the code it documents.

`jsa/pipeline/prompt_assembly.py::_tool_contract` deliberately does NOT read
docs/TOOLS.md — the runtime prompt path must not be able to break on a missing or
malformed markdown file, so the contract is single-sourced from `jsa/agents/tool_spec.py`
instead. That decision leaves the doc free to rot silently, which is what this module
exists to prevent: it is the anti-drift guarantee the plan wanted, moved from the runtime
path to the test suite.

Everything asserted here is MECHANICAL — a name, a code, a number, a flag. Prose is not
checked and is not meant to be; the point is that a reader of the doc can trust its
tables, not its adjectives.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jsa.agents.base import AgentBackend, ToolsUnsupported
from jsa.agents.tool_spec import tools_for
from jsa.db.models import Stage
from jsa.pipeline.prompt_assembly import _TOOL_ERROR_CODES
from jsa.pipeline.tool_loop import TOOL_BUDGET

_DOC = Path(__file__).resolve().parents[2] / "docs" / "TOOLS.md"


@pytest.fixture(scope="module")
def doc() -> str:
    assert _DOC.exists(), (
        f"{_DOC} is missing. It is the human-readable reference for the revision tool "
        "vocabulary and is required — see the module docstring."
    )
    return _DOC.read_text(encoding="utf-8")


def _documented_tool_names(doc: str) -> set[str]:
    """Every ``| `name` |`` cell opening a row in one of the two vocabulary tables.

    Scoped to those sections deliberately — the error-code table further up uses the
    same row shape, and a doc-wide scan would read `unknown_id` and friends as tool
    names.
    """
    names: set[str] = set()
    for heading in ("## CV vocabulary", "## Cover-letter vocabulary"):
        start = doc.index(heading)
        end = doc.find("\n## ", start + len(heading))
        section = doc[start:] if end == -1 else doc[start:end]
        names |= set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))
    return names


class TestToolVocabularyIsFullyDocumented:
    @pytest.mark.parametrize("stage", [Stage.revising_cv, Stage.revising_cl])
    def test_every_tool_in_the_stage_vocabulary_has_a_table_row(self, doc, stage):
        documented = _documented_tool_names(doc)
        missing = {spec.name for spec in tools_for(stage)} - documented
        assert not missing, (
            f"docs/TOOLS.md has no table row for {sorted(missing)} ({stage.value}). "
            "Add the tool to the vocabulary table there as well as to tool_spec.py."
        )

    def test_the_doc_documents_no_tool_that_does_not_exist(self, doc):
        real = {spec.name for spec in tools_for(Stage.revising_cv)} | {
            spec.name for spec in tools_for(Stage.revising_cl)
        }
        extra = _documented_tool_names(doc) - real
        assert not extra, (
            f"docs/TOOLS.md documents {sorted(extra)}, which tool_spec.py does not "
            "define. Either the tool was removed and the doc wasn't, or the name is "
            "misspelled."
        )

    @pytest.mark.parametrize("stage", [Stage.revising_cv, Stage.revising_cl])
    def test_every_required_parameter_is_named_in_the_doc(self, doc, stage):
        for spec in tools_for(stage):
            for param in (spec.parameters or {}).get("required", []):
                assert f"`{param}`" in doc, (
                    f"docs/TOOLS.md never mentions `{param}`, a REQUIRED parameter of "
                    f"{spec.name!r}. A reader following the doc would omit it and get "
                    "a bad_argument result."
                )


class TestResultContractIsFullyDocumented:
    def test_every_error_code_has_a_row_in_the_error_table(self, doc):
        for code in _TOOL_ERROR_CODES:
            assert re.search(rf"^\| `{re.escape(code)}` \|", doc, re.MULTILINE), (
                f"docs/TOOLS.md's error-code table has no row for {code!r}. That code is "
                "in _TOOL_ERROR_CODES, so the model is told about it at runtime — the "
                "doc must explain it too."
            )

    def test_the_doc_invents_no_error_code(self, doc):
        table = re.search(r"### Error codes\n\n(.*?)\n\n", doc, re.DOTALL)
        assert table is not None, "docs/TOOLS.md has no '### Error codes' table"
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", table.group(1), re.MULTILINE))
        assert documented == set(_TOOL_ERROR_CODES), (
            f"error-code drift: doc has {sorted(documented)}, code has "
            f"{sorted(_TOOL_ERROR_CODES)}"
        )


class TestBudgetAndLadderFactsAreCurrent:
    def test_the_documented_call_budget_matches_TOOL_BUDGET(self, doc):
        """EVERY place the doc states the budget must be current — an `or` here would let
        one stale mention survive because another spot happened to be right."""
        for phrasing in (f"**{TOOL_BUDGET}** tool calls", f"{TOOL_BUDGET}-call budget"):
            assert phrasing in doc, (
                f"docs/TOOLS.md is missing {phrasing!r}. tool_loop.py::TOOL_BUDGET "
                f"({TOOL_BUDGET}) is the source of truth and the doc states the budget "
                "in more than one place — all of them must agree."
            )
        # And no OTHER number is presented as the budget anywhere.
        stale = [
            n for n in re.findall(r"\*\*(\d+)\*\* tool calls|(\d+)-call budget", doc)
            for n in n if n and int(n) != TOOL_BUDGET
        ]
        assert not stale, (
            f"docs/TOOLS.md states a budget of {stale} somewhere; TOOL_BUDGET is "
            f"{TOOL_BUDGET}."
        )

    def test_backends_that_skip_the_ladder_are_named(self, doc):
        """The two backends _tools_for gates out must be named in the doc — a reader
        debugging "why did my revision not use tools" looks here first."""
        from jsa.agents.claude_cli import ClaudeCliBackend
        from jsa.agents.google_cli import GoogleCliBackend

        for backend in (ClaudeCliBackend, GoogleCliBackend):
            assert backend.restore_applies_system_prompt is False
            assert backend.name in doc, (
                f"{backend.name} cannot reach either tool rung "
                "(restore_applies_system_prompt is False) but docs/TOOLS.md never says so."
            )

    def test_every_backend_the_doc_calls_native_actually_is(self, doc):
        """The native-rung row lists backend names; each must really set
        supports_native_tools. A stale name here would send a reader chasing a rung that
        backend never enters."""
        from jsa.agents.anthropic_api import AnthropicAPIBackend
        from jsa.agents.gemini_api import GeminiBackend
        from jsa.agents.mistral import MistralBackend
        from jsa.agents.opencode_zen import OpenCodeZenBackend
        from jsa.agents.openrouter import OpenRouterBackend

        native_row = next(
            line for line in doc.splitlines() if line.startswith("| 1 | **native**")
        )
        for cls in (
            AnthropicAPIBackend,
            GeminiBackend,
            MistralBackend,
            OpenCodeZenBackend,
            OpenRouterBackend,
        ):
            if cls.name in native_row:
                assert cls.supports_native_tools is True, (
                    f"docs/TOOLS.md lists {cls.name} as a native-rung backend, but "
                    "supports_native_tools is False."
                )

    def test_the_flag_default_the_doc_relies_on_is_still_true(self):
        """The doc's ladder table says rung 2 reaches 'any backend whose restore_session
        actually applies the system prompt' — which only holds while the base default is
        opt-OUT, not opt-in."""
        assert AgentBackend.restore_applies_system_prompt is True


class TestExclusionClaimsAreTrue:
    """The doc's "Mutual exclusions" section makes load-bearing claims about exception
    hierarchy. These are exactly the claims that rot silently, so pin them."""

    def test_every_backend_tools_rejection_is_a_ToolsUnsupported_not_a_BF19_signal(self):
        from jsa.agents.base import AgentBackendUnavailable

        import jsa.agents._openai_compat as oc
        import jsa.agents.anthropic_api as anth
        import jsa.agents.gemini_api as gem
        import jsa.agents.opencode_zen as zen

        found = []
        for module in (oc, anth, gem, zen):
            rejected = getattr(module, "_ToolsRejected", None)
            if rejected is None:
                continue
            found.append(f"{module.__name__}._ToolsRejected")
            assert issubclass(rejected, ToolsUnsupported), (
                f"{module.__name__}._ToolsRejected must subclass ToolsUnsupported so a "
                "tools-only degrade costs this turn its tools, not the job its BF-19 slot."
            )
            assert not issubclass(rejected, AgentBackendUnavailable), (
                f"{module.__name__}._ToolsRejected subclasses AgentBackendUnavailable — "
                "that routes a tools-only rejection into _advance_backend_or_fail and "
                "advances the whole job."
            )
        assert found, "no _ToolsRejected found in any backend — has it been renamed?"

    def test_the_doc_is_right_that_the_loop_never_streams(self):
        """tool_loop.py must not pass on_chunk/on_retry to any backend call."""
        source = (
            Path(__file__).resolve().parents[2] / "jsa" / "pipeline" / "tool_loop.py"
        ).read_text(encoding="utf-8")
        for kwarg in ("on_chunk=", "on_retry="):
            assert kwarg not in source, (
                f"tool_loop.py passes {kwarg} somewhere — docs/TOOLS.md claims tool mode "
                "never streams, and the loop's own docstring depends on it."
            )
