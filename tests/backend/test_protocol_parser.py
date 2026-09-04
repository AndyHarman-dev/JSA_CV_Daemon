"""Tests for jsa.agents.protocol: parse_reply() sentinel parser.

Covers all six resolution rules documented in ARCH.md / CLAUDE.md:
  1. Exactly one FINAL block → valid
  2. Exactly one NEED_INPUT block → valid
  3. Zero complete blocks → ProtocolError("no sentinel block")
  4. Multiple blocks → last wins (log warning emitted)
  5. Unclosed sentinel (opener, no <<<END>>>) → ProtocolError("unterminated block")
  6. Nested sentinels not supported; <<<END>>> terminates regardless
"""

from __future__ import annotations

import logging

import pytest

from jsa.agents.protocol import ProtocolError, parse_reply


# ---------------------------------------------------------------------------
# 1 — Single FINAL block
# ---------------------------------------------------------------------------

class TestSingleFinalBlock:
    def test_kind_is_final(self):
        raw = "<<<FINAL>>>\nHere is the result.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.kind == "final"

    def test_content_is_inner_text(self):
        raw = "<<<FINAL>>>\nHere is the result.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.content == "Here is the result."

    def test_question_is_none(self):
        raw = "<<<FINAL>>>\nHere is the result.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.question is None

    def test_raw_equals_full_input(self):
        raw = "<<<FINAL>>>\nHere is the result.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.raw == raw

    def test_content_stripped_of_outer_whitespace(self):
        raw = "<<<FINAL>>>\n   inner   \n<<<END>>>"
        reply = parse_reply(raw)
        # .strip() removes outer whitespace including leading/trailing newlines
        assert reply.content == "inner"


# ---------------------------------------------------------------------------
# 2 — Single NEED_INPUT block
# ---------------------------------------------------------------------------

class TestSingleNeedInputBlock:
    def test_kind_is_needs_input(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.kind == "needs_input"

    def test_content_set_to_inner_text(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.content == "What is your target role?"

    def test_question_equals_content(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.question == reply.content
        assert reply.question == "What is your target role?"

    def test_raw_equals_full_input(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.raw == raw


# ---------------------------------------------------------------------------
# 3 — Zero complete blocks → ProtocolError("no sentinel block")
# ---------------------------------------------------------------------------

class TestZeroBlocks:
    def test_plain_text_raises_protocol_error(self):
        with pytest.raises(ProtocolError, match="no sentinel block"):
            parse_reply("This is just a plain text reply with no sentinels.")

    def test_empty_string_raises_protocol_error(self):
        with pytest.raises(ProtocolError, match="no sentinel block"):
            parse_reply("")

    def test_whitespace_only_raises_protocol_error(self):
        with pytest.raises(ProtocolError, match="no sentinel block"):
            parse_reply("   \n\n   ")

    def test_similar_but_wrong_markers_raise(self):
        # Markers with wrong bracket style should not match
        with pytest.raises(ProtocolError, match="no sentinel block"):
            parse_reply("<<FINAL>>\nsome content\n<<END>>")


# ---------------------------------------------------------------------------
# 4 — Multiple complete blocks → last wins
# ---------------------------------------------------------------------------

class TestMultipleBlocks:
    def test_last_block_kind_wins_final_then_needs_input(self):
        raw = (
            "<<<FINAL>>>\nFirst output.\n<<<END>>>\n"
            "<<<NEED_INPUT>>>\nWhat is your role?\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "needs_input"
        assert reply.content == "What is your role?"

    def test_last_block_kind_wins_needs_input_then_final(self):
        raw = (
            "<<<NEED_INPUT>>>\nWhat is your role?\n<<<END>>>\n"
            "<<<FINAL>>>\nFinal output.\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "Final output."

    def test_multiple_same_kind_last_content_wins(self):
        raw = (
            "<<<FINAL>>>\nFirst content.\n<<<END>>>\n"
            "<<<FINAL>>>\nSecond content.\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "Second content."

    def test_multiple_need_input_last_content_wins(self):
        raw = (
            "<<<NEED_INPUT>>>\nFirst question.\n<<<END>>>\n"
            "<<<NEED_INPUT>>>\nSecond question.\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "needs_input"
        assert reply.content == "Second question."
        assert reply.question == "Second question."

    def test_raw_still_equals_full_input_with_multiple_blocks(self):
        raw = (
            "<<<FINAL>>>\nFirst output.\n<<<END>>>\n"
            "<<<NEED_INPUT>>>\nWhat is your role?\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.raw == raw

    def test_warning_logged_for_mixed_kind_multiple_blocks(self, caplog):
        """ARCH.md rule 4: multiple blocks must trigger a WARNING from jsa.agents.protocol."""
        raw = (
            "<<<FINAL>>>\nfirst\n<<<END>>>\n"
            "<<<NEED_INPUT>>>\nsecond\n<<<END>>>"
        )
        with caplog.at_level(logging.WARNING, logger="jsa.agents.protocol"):
            parse_reply(raw)
        warning_records = [
            r for r in caplog.records
            if r.name == "jsa.agents.protocol" and r.levelno == logging.WARNING
        ]
        assert warning_records, "expected a WARNING from jsa.agents.protocol"
        assert any(
            "multiple" in r.getMessage().lower() or "sentinel" in r.getMessage().lower()
            for r in warning_records
        )

    def test_warning_logged_for_same_kind_multiple_blocks(self, caplog):
        """ARCH.md rule 4: warning fires even when both blocks are the same kind."""
        raw = (
            "<<<FINAL>>>\nFirst content.\n<<<END>>>\n"
            "<<<FINAL>>>\nSecond content.\n<<<END>>>"
        )
        with caplog.at_level(logging.WARNING, logger="jsa.agents.protocol"):
            parse_reply(raw)
        warning_records = [
            r for r in caplog.records
            if r.name == "jsa.agents.protocol" and r.levelno == logging.WARNING
        ]
        assert warning_records, "expected a WARNING from jsa.agents.protocol"
        assert any(
            "multiple" in r.getMessage().lower() or "sentinel" in r.getMessage().lower()
            for r in warning_records
        )


# ---------------------------------------------------------------------------
# 5 — Unclosed FINAL sentinel → ProtocolError("unterminated block")
# ---------------------------------------------------------------------------

class TestUnclosedFinalSentinel:
    def test_final_without_end_raises(self):
        raw = "<<<FINAL>>>\nThis content has no closing marker."
        with pytest.raises(ProtocolError, match="unterminated block"):
            parse_reply(raw)

    def test_final_with_only_partial_end_raises(self):
        # Has the opener but no <<<END>>>
        raw = "Preamble text.\n<<<FINAL>>>\nContent here."
        with pytest.raises(ProtocolError, match="unterminated block"):
            parse_reply(raw)


# ---------------------------------------------------------------------------
# 6 — Unclosed NEED_INPUT sentinel → ProtocolError("unterminated block")
# ---------------------------------------------------------------------------

class TestUnclosedNeedInputSentinel:
    def test_need_input_without_end_raises(self):
        raw = "<<<NEED_INPUT>>>\nWhat should I include?"
        with pytest.raises(ProtocolError, match="unterminated block"):
            parse_reply(raw)

    def test_need_input_in_middle_without_end_raises(self):
        raw = "Here is some preamble.\n<<<NEED_INPUT>>>\nMissing close."
        with pytest.raises(ProtocolError, match="unterminated block"):
            parse_reply(raw)


# ---------------------------------------------------------------------------
# Multi-line content preservation
# ---------------------------------------------------------------------------

class TestMultiLineContent:
    def test_internal_newlines_preserved_in_final(self):
        raw = "<<<FINAL>>>\nLine one.\nLine two.\nLine three.\n<<<END>>>"
        reply = parse_reply(raw)
        # Internal newlines must be preserved; only outer whitespace stripped
        assert "Line one." in reply.content
        assert "Line two." in reply.content
        assert "Line three." in reply.content
        assert "\n" in reply.content

    def test_internal_newlines_preserved_in_need_input(self):
        raw = "<<<NEED_INPUT>>>\nFirst question?\nSecond question?\n<<<END>>>"
        reply = parse_reply(raw)
        assert "First question?" in reply.content
        assert "Second question?" in reply.content
        assert "\n" in reply.content

    def test_markdown_content_preserved(self):
        markdown = "# Adjusted CV\n\n## Experience\n\n- Item 1\n- Item 2"
        raw = f"<<<FINAL>>>\n{markdown}\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.content == markdown


# ---------------------------------------------------------------------------
# Surrounding text is ignored
# ---------------------------------------------------------------------------

class TestSurroundingTextIgnored:
    def test_text_before_block_not_in_content(self):
        raw = "Some preamble text.\n<<<FINAL>>>\nThe actual content.\n<<<END>>>"
        reply = parse_reply(raw)
        assert "preamble" not in reply.content
        assert reply.content == "The actual content."

    def test_text_after_block_not_in_content(self):
        raw = "<<<FINAL>>>\nThe actual content.\n<<<END>>>\nSome trailing text."
        reply = parse_reply(raw)
        assert "trailing" not in reply.content
        assert reply.content == "The actual content."

    def test_text_before_and_after_block_both_ignored(self):
        raw = (
            "Agent reasoning: I'll format this now.\n"
            "<<<NEED_INPUT>>>\n"
            "What industries interest you?\n"
            "<<<END>>>\n"
            "End of agent turn."
        )
        reply = parse_reply(raw)
        assert reply.content == "What industries interest you?"


# ---------------------------------------------------------------------------
# raw field always equals full input
# ---------------------------------------------------------------------------

class TestRawField:
    def test_raw_preserved_for_final(self):
        raw = "Preamble\n<<<FINAL>>>\ncontent\n<<<END>>>\nPostamble"
        reply = parse_reply(raw)
        assert reply.raw == raw

    def test_raw_preserved_for_need_input(self):
        raw = "Thinking...\n<<<NEED_INPUT>>>\nQuestion here\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.raw == raw

    def test_raw_preserved_verbatim_no_mutation(self):
        raw = "<<<FINAL>>>\ncontent with   spaces   \n<<<END>>>"
        reply = parse_reply(raw)
        # raw is not stripped — only content is
        assert raw in reply.raw
        assert reply.raw == raw


# ---------------------------------------------------------------------------
# NEED_INPUT question field contract
# ---------------------------------------------------------------------------

class TestNeedInputQuestionField:
    def test_question_is_same_object_reference_as_content(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target industry?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.question == reply.content

    def test_question_is_none_for_final(self):
        raw = "<<<FINAL>>>\nFinal result here.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.question is None

    def test_question_populated_for_multi_line_need_input(self):
        raw = "<<<NEED_INPUT>>>\nLine 1?\nLine 2?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.question is not None
        assert "Line 1?" in reply.question
        assert "Line 2?" in reply.question


# ---------------------------------------------------------------------------
# Optional <<<SUGGESTIONS>>> block inside a NEED_INPUT body (agent chat upgrade
# Phase 4) — pure superset addition, absent by default.
# ---------------------------------------------------------------------------

class TestSuggestionsBlock:
    def test_absent_suggestions_is_none(self):
        raw = "<<<NEED_INPUT>>>\nWhat is your target role?\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.suggested_replies is None
        assert reply.question == "What is your target role?"

    def test_present_suggestions_parsed_and_stripped_from_question(self):
        raw = (
            "<<<NEED_INPUT>>>\n"
            "Which dates should I use for the last role?\n"
            "<<<SUGGESTIONS>>>\n"
            "Use 2019-present\n"
            "Use 2019-2023\n"
            "Let me check and get back to you\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.question == "Which dates should I use for the last role?"
        assert reply.content == reply.question
        assert reply.suggested_replies == [
            "Use 2019-present",
            "Use 2019-2023",
            "Let me check and get back to you",
        ]

    def test_final_block_never_gets_suggestions(self):
        raw = "<<<FINAL>>>\nFinal result here.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.suggested_replies is None

    def test_empty_suggestions_block_yields_none(self):
        raw = (
            "<<<NEED_INPUT>>>\n"
            "What is your target role?\n"
            "<<<SUGGESTIONS>>>\n"
            "\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.suggested_replies is None


# ---------------------------------------------------------------------------
# TOOL_CALLS block (revision-tool-use plan, Phase 2 — the prompt rung's transport)
# ---------------------------------------------------------------------------

class TestToolCallsBlock:
    def test_kind_is_tool_calls(self):
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        reply = parse_reply(raw)
        assert reply.kind == "tool_calls"

    def test_single_call_parsed(self):
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        reply = parse_reply(raw)
        assert reply.tool_calls is not None
        assert len(reply.tool_calls) == 1
        assert reply.tool_calls[0].id == "call_0"
        assert reply.tool_calls[0].name == "get_cv"
        assert reply.tool_calls[0].arguments == {}

    def test_multiple_calls_preserve_array_order_and_synthesize_sequential_ids(self):
        raw = (
            "<<<TOOL_CALLS>>>\n"
            '[{"name": "get_cv", "arguments": {}}, '
            '{"name": "replace_summary", "arguments": {"text": "New summary."}}]\n'
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert [c.id for c in reply.tool_calls] == ["call_0", "call_1"]
        assert [c.name for c in reply.tool_calls] == ["get_cv", "replace_summary"]
        assert reply.tool_calls[1].arguments == {"text": "New summary."}

    def test_other_fields_are_none_or_content_only(self):
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        reply = parse_reply(raw)
        assert reply.question is None
        assert reply.suggested_replies is None
        assert reply.raw == raw

    def test_invalid_json_raises_protocol_error(self):
        raw = "<<<TOOL_CALLS>>>\nnot json\n<<<END>>>"
        with pytest.raises(ProtocolError, match="TOOL_CALLS"):
            parse_reply(raw)

    def test_non_array_body_raises_protocol_error(self):
        raw = '<<<TOOL_CALLS>>>\n{"name": "get_cv", "arguments": {}}\n<<<END>>>'
        with pytest.raises(ProtocolError, match="non-empty JSON array"):
            parse_reply(raw)

    def test_empty_array_raises_protocol_error(self):
        raw = "<<<TOOL_CALLS>>>\n[]\n<<<END>>>"
        with pytest.raises(ProtocolError, match="non-empty JSON array"):
            parse_reply(raw)

    def test_item_missing_name_raises_protocol_error(self):
        raw = '<<<TOOL_CALLS>>>\n[{"arguments": {}}]\n<<<END>>>'
        with pytest.raises(ProtocolError, match="item 0"):
            parse_reply(raw)

    def test_item_with_non_object_arguments_raises_protocol_error(self):
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": "nope"}]\n<<<END>>>'
        with pytest.raises(ProtocolError, match="item 0"):
            parse_reply(raw)

    def test_unterminated_tool_calls_block_raises_protocol_error(self):
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]'
        with pytest.raises(ProtocolError, match="unterminated block"):
            parse_reply(raw)


class TestToolCallsPrecedence:
    """A NEED_INPUT/FINAL block always outranks a TOOL_CALLS block, whatever the order.

    TOOL_CALLS is matched unconditionally for every session on every backend (parse_reply
    has no session context — the prompt rung passes no ``tools=`` kwarg, so a sentinel-only
    backend gets no signal). Without this precedence rule the plain "multiple blocks -> take
    the last" resolution lets a stray trailing TOOL_CALLS block in a NON-tool session
    (cv_adjust / cover_letter / fit_assessment) silently outvote the model's real answer
    and hard-fail on run_stage's unexpected-tool-call guard.
    """

    def test_trailing_tool_calls_does_not_outvote_a_leading_final(self):
        raw = (
            "<<<FINAL>>>\nThe real answer.\n<<<END>>>\n"
            '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "The real answer."

    def test_trailing_tool_calls_does_not_outvote_a_leading_need_input(self):
        raw = (
            "<<<NEED_INPUT>>>\nWhich role?\n<<<END>>>\n"
            '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        )
        reply = parse_reply(raw)
        assert reply.kind == "needs_input"
        assert reply.question == "Which role?"

    def test_leading_tool_calls_still_loses_to_a_trailing_final(self):
        """Consistency check: precedence is by KIND, not position, in both directions."""
        raw = (
            '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>\n'
            "<<<FINAL>>>\nThe real answer.\n<<<END>>>"
        )
        assert parse_reply(raw).kind == "final"

    def test_last_of_several_non_tool_blocks_still_wins(self):
        """The pre-existing rule-4 behavior is unchanged among NEED_INPUT/FINAL blocks."""
        raw = (
            "<<<FINAL>>>\nFirst.\n<<<END>>>\n"
            '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>\n'
            "<<<FINAL>>>\nSecond.\n<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "Second."

    def test_tool_calls_alone_still_parses_as_tool_calls(self):
        """Constraint the fix must NOT break: a prompt-rung (rung 2) reply consisting only
        of a TOOL_CALLS block must not raise ProtocolError("no sentinel block") — that is
        what claude_cli/opencode_zen's _parse_with_nudge keys on, and a nudge here would
        burn a turn re-prompting for a sentinel the model was correctly told not to emit.
        """
        raw = '<<<TOOL_CALLS>>>\n[{"name": "get_cv", "arguments": {}}]\n<<<END>>>'
        reply = parse_reply(raw)
        assert reply.kind == "tool_calls"
        assert reply.tool_calls is not None and len(reply.tool_calls) == 1
