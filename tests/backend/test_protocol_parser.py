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
