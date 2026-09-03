"""jsa/agents/tool_spec.py — tool vocabulary + provider-shape renderers.

Revision-tool-use plan, Phase 1.
"""

from __future__ import annotations

import pytest

from jsa.agents.tool_spec import (
    CL_TOOL_SPECS,
    CV_TOOL_SPECS,
    SHARED_TOOL_SPECS,
    ToolSpec,
    to_anthropic_tools,
    to_gemini_function_declarations,
    to_openai_tools,
    tools_for,
)
from jsa.db.models import Stage


class TestToolsForScope:
    """D3: tools exist ONLY for revising_cv/revising_cl."""

    def test_revising_cv_gets_cv_tools_plus_shared(self):
        specs = tools_for(Stage.revising_cv)
        names = {s.name for s in specs}
        assert names == {s.name for s in CV_TOOL_SPECS} | {s.name for s in SHARED_TOOL_SPECS}

    def test_revising_cl_gets_cl_tools_plus_shared(self):
        specs = tools_for(Stage.revising_cl)
        names = {s.name for s in specs}
        assert names == {s.name for s in CL_TOOL_SPECS} | {s.name for s in SHARED_TOOL_SPECS}

    @pytest.mark.parametrize(
        "stage",
        [Stage.cv_adjust, Stage.cover_letter, Stage.fit_assessment],
    )
    def test_every_other_stage_raises(self, stage):
        with pytest.raises(ValueError, match="revision-only"):
            tools_for(stage)

    def test_finalize_and_ask_user_present_in_both_groups(self):
        for stage in (Stage.revising_cv, Stage.revising_cl):
            names = {s.name for s in tools_for(stage)}
            assert "finalize" in names
            assert "ask_user" in names

    def test_no_duplicate_names_within_a_stage(self):
        for stage in (Stage.revising_cv, Stage.revising_cl):
            names = [s.name for s in tools_for(stage)]
            assert len(names) == len(set(names))


class TestSchemasAreFlatAndRefFree:
    def _assert_no_refs(self, node) -> None:
        if isinstance(node, dict):
            assert "$ref" not in node
            assert "$defs" not in node
            for v in node.values():
                self._assert_no_refs(v)
        elif isinstance(node, list):
            for v in node:
                self._assert_no_refs(v)

    def test_every_tool_parameter_schema_is_ref_free(self):
        for spec in CV_TOOL_SPECS + CL_TOOL_SPECS + SHARED_TOOL_SPECS:
            self._assert_no_refs(spec.parameters)

    def test_every_tool_parameter_schema_is_a_flat_object(self):
        for spec in CV_TOOL_SPECS + CL_TOOL_SPECS + SHARED_TOOL_SPECS:
            assert spec.parameters["type"] == "object"
            assert "properties" in spec.parameters


class TestAnthropicRenderer:
    def test_shape(self):
        rendered = to_anthropic_tools(tools_for(Stage.revising_cv))
        assert rendered
        for tool in rendered:
            assert set(tool.keys()) == {"name", "description", "input_schema"}
            assert tool["input_schema"]["type"] == "object"

    def test_names_match_specs(self):
        specs = tools_for(Stage.revising_cv)
        rendered = to_anthropic_tools(specs)
        assert [t["name"] for t in rendered] == [s.name for s in specs]


class TestOpenAIRenderer:
    def test_shape(self):
        rendered = to_openai_tools(tools_for(Stage.revising_cl))
        assert rendered
        for tool in rendered:
            assert tool["type"] == "function"
            fn = tool["function"]
            assert set(fn.keys()) == {"name", "description", "parameters"}
            assert fn["parameters"]["type"] == "object"

    def test_names_match_specs(self):
        specs = tools_for(Stage.revising_cl)
        rendered = to_openai_tools(specs)
        assert [t["function"]["name"] for t in rendered] == [s.name for s in specs]


class TestGeminiRenderer:
    def test_shape(self):
        rendered = to_gemini_function_declarations(tools_for(Stage.revising_cv))
        assert rendered
        for decl in rendered:
            assert set(decl.keys()) == {"name", "description", "parameters"}

    def test_strips_additional_properties_recursively(self):
        rendered = to_gemini_function_declarations(tools_for(Stage.revising_cv))

        def _assert_no_additional_properties(node) -> None:
            if isinstance(node, dict):
                assert "additionalProperties" not in node
                for v in node.values():
                    _assert_no_additional_properties(v)
            elif isinstance(node, list):
                for v in node:
                    _assert_no_additional_properties(v)

        for decl in rendered:
            _assert_no_additional_properties(decl["parameters"])

    def test_does_not_mutate_the_source_spec(self):
        specs = tools_for(Stage.revising_cv)
        to_gemini_function_declarations(specs)
        # Anthropic's renderer (which keeps additionalProperties) must still see it —
        # proves the gemini renderer copied rather than mutated the shared ToolSpec.
        rendered = to_anthropic_tools(specs)
        replace_section = next(t for t in rendered if t["name"] == "replace_section")
        assert "additionalProperties" in replace_section["input_schema"]


class TestToolSpecIsFrozen:
    def test_cannot_mutate_a_spec(self):
        spec = CV_TOOL_SPECS[0]
        assert isinstance(spec, ToolSpec)
        with pytest.raises(Exception):
            spec.name = "hacked"  # type: ignore[misc]
