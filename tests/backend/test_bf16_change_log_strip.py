"""Tests for BF-16: _strip_change_log helper called from parse_reply on FINAL blocks.

All tests are pure unit tests using parse_reply from jsa.agents.protocol —
no async, no DB, no backend needed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsa.agents.protocol import parse_reply


# ---------------------------------------------------------------------------
# 1 — XML <change_log>...</change_log> stripped from FINAL content
# ---------------------------------------------------------------------------

class TestXmlChangeLogStripped:
    def test_xml_block_removed_from_final_content(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "<change_log>\n"
            "- Changed X\n"
            "</change_log>\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "# Name\nSome CV"

    def test_xml_block_not_in_content(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "<change_log>\n"
            "- Changed X\n"
            "</change_log>\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert "<change_log>" not in reply.content
        assert "</change_log>" not in reply.content


# ---------------------------------------------------------------------------
# 2 — Markdown ## Change Log heading stripped from FINAL content
# ---------------------------------------------------------------------------

class TestMarkdownH2ChangeLogStripped:
    def test_h2_heading_and_following_text_removed(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "## Change Log\n"
            "- Changed X\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "# Name\nSome CV"

    def test_h2_heading_not_in_content(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "## Change Log\n"
            "- Changed X\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert "## Change Log" not in reply.content
        assert "Changed X" not in reply.content


# ---------------------------------------------------------------------------
# 3 — Markdown ### Change Log heading stripped from FINAL content
# ---------------------------------------------------------------------------

class TestMarkdownH3ChangeLogStripped:
    def test_h3_heading_and_following_text_removed(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "### Change Log\n"
            "- Changed X\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        assert reply.content == "# Name\nSome CV"

    def test_h3_heading_not_in_content(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Name\n"
            "Some CV\n"
            "\n"
            "### Change Log\n"
            "- Changed X\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert "### Change Log" not in reply.content
        assert "Changed X" not in reply.content


# ---------------------------------------------------------------------------
# 4 — No Change Log present — content unchanged
# ---------------------------------------------------------------------------

class TestNoChangeLogContentUnchanged:
    def test_clean_final_block_content_unchanged(self):
        raw = (
            "<<<FINAL>>>\n"
            "# Jane Smith\n"
            "\n"
            "## Experience\n"
            "\n"
            "- Built things\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "final"
        expected = "# Jane Smith\n\n## Experience\n\n- Built things"
        assert reply.content == expected

    def test_clean_final_block_no_stripping_side_effects(self):
        raw = "<<<FINAL>>>\n# CV Content\nSome text here.\n<<<END>>>"
        reply = parse_reply(raw)
        assert reply.content == "# CV Content\nSome text here."


# ---------------------------------------------------------------------------
# 5 — NEED_INPUT content is never stripped
# ---------------------------------------------------------------------------

class TestNeedInputNotStripped:
    def test_change_log_xml_preserved_in_need_input(self):
        """_strip_change_log is only applied to FINAL blocks.
        A NEED_INPUT block whose question text contains <change_log>...</change_log>
        must NOT have that text removed."""
        raw = (
            "<<<NEED_INPUT>>>\n"
            "Please review the following change log before proceeding:\n"
            "<change_log>\n"
            "- Added Python skills\n"
            "</change_log>\n"
            "Do you approve these changes?\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.kind == "needs_input"
        # change_log content must be present in both content and question
        assert "<change_log>" in reply.content
        assert "</change_log>" in reply.content
        assert "Added Python skills" in reply.content
        assert reply.question is not None
        assert "<change_log>" in reply.question
        assert "</change_log>" in reply.question
        assert "Added Python skills" in reply.question

    def test_change_log_xml_in_need_input_question_equals_content(self):
        """For NEED_INPUT, question and content are the same value."""
        raw = (
            "<<<NEED_INPUT>>>\n"
            "<change_log>- item</change_log>\n"
            "<<<END>>>"
        )
        reply = parse_reply(raw)
        assert reply.question == reply.content
        assert "<change_log>" in reply.question


# ---------------------------------------------------------------------------
# 6 — Prompt sanity check: PROMPT_CDADJUST.md instructs model correctly
# ---------------------------------------------------------------------------

class TestPromptSanityCheck:
    """Read PROMPT_CDADJUST.md and assert it carries the Change Log placement
    instructions introduced in BF-16."""

    _PROMPT_PATH = (
        Path(__file__).parent.parent.parent
        / "jsa" / "prompts" / "PROMPT_CDADJUST.md"
    )

    def _read_prompt(self) -> str:
        return self._PROMPT_PATH.read_text(encoding="utf-8")

    def test_prohibition_present(self):
        """The prompt must explicitly prohibit the Change Log from being inside FINAL."""
        prompt = self._read_prompt()
        # The prohibition is expressed as "Do NOT include the Change Log inside the"
        assert "Do NOT include the Change Log inside the" in prompt, (
            "Expected an explicit prohibition against placing the Change Log "
            "inside the <<<FINAL>>> block."
        )

    def test_positive_instruction_to_place_before_final(self):
        """The prompt must positively instruct the model to write the Change Log
        BEFORE the <<<FINAL>>> sentinel, not inside it.

        We check for the phrase 'before' adjacent to '<<<FINAL>>>' in the
        context of the Change Log step, which is the key new instruction in BF-16.
        """
        prompt = self._read_prompt()
        # The instruction should contain "before" and "<<<FINAL>>>" near each other
        # in the Change Log step. We look for "before" within 120 chars of "<<<FINAL>>>".
        import re
        # Find any occurrence of "before" that appears within 120 characters of "<<<FINAL>>>"
        found = re.search(
            r"before.{0,120}<<<FINAL>>>|<<<FINAL>>>.{0,120}before",
            prompt,
            re.IGNORECASE | re.DOTALL,
        )
        assert found is not None, (
            "Expected the prompt to instruct the model to write the Change Log "
            "BEFORE the <<<FINAL>>> sentinel (BF-16 requirement)."
        )

    def test_no_positive_instruction_to_put_change_log_inside_final(self):
        """There must be no positive (non-negated) instruction telling the model
        to write the Change Log inside the FINAL block.

        Scans for patterns like 'write/place/put/include the Change Log inside'
        (without a leading 'Do NOT' / 'not' negation).
        """
        import re
        prompt = self._read_prompt()
        # Match a positive instruction (without "not" or "do not" immediately before)
        # e.g. "include the change log inside" / "place the change log inside"
        positive_instruction = re.compile(
            r"(?<!not\s)(?<!NOT\s)(?<!Do NOT\s)"
            r"\b(write|place|put|include)\b.{0,30}[Cc]hange\s+[Ll]og\s+inside",
            re.IGNORECASE | re.DOTALL,
        )
        match = positive_instruction.search(prompt)
        assert match is None, (
            f"Found a positive instruction to place the Change Log inside FINAL: "
            f"{match.group(0)!r}. BF-16 requires the Change Log to go BEFORE "
            f"the <<<FINAL>>> block."
        )


# ---------------------------------------------------------------------------
# 7 — XML <change_log> in the middle of document: content after block survives
# ---------------------------------------------------------------------------

class TestXmlChangeLogMidDocumentContentSurvives:
    """An XML <change_log> block placed mid-document (not at the end) must be
    stripped without destroying the CV sections that follow it."""

    _RAW = (
        "<<<FINAL>>>\n"
        "# Jane Smith\n"
        "\n"
        "## Experience\n"
        "\n"
        "Senior Engineer at Acme\n"
        "\n"
        "<change_log>\n"
        "- Changed X\n"
        "- Changed Y\n"
        "</change_log>\n"
        "\n"
        "## Education\n"
        "\n"
        "BS Computer Science\n"
        "<<<END>>>"
    )

    def test_cv_sections_before_xml_block_survive(self):
        reply = parse_reply(self._RAW)
        assert reply.kind == "final"
        assert "# Jane Smith" in reply.content
        assert "## Experience" in reply.content
        assert "Senior Engineer at Acme" in reply.content

    def test_xml_block_removed_from_mid_document(self):
        reply = parse_reply(self._RAW)
        assert "<change_log>" not in reply.content
        assert "</change_log>" not in reply.content
        assert "Changed X" not in reply.content

    def test_cv_sections_after_xml_block_survive(self):
        reply = parse_reply(self._RAW)
        assert "## Education" in reply.content
        assert "BS Computer Science" in reply.content


# ---------------------------------------------------------------------------
# 8 — Markdown ## Change Log heading mid-document: sections after it survive
# ---------------------------------------------------------------------------

class TestMarkdownChangeLogMidDocumentSectionsSurvive:
    """A ## Change Log heading placed mid-document (not at the end) must be
    stripped along with its body, but CV sections that follow the next heading
    must survive intact."""

    _RAW = (
        "<<<FINAL>>>\n"
        "# Jane Smith\n"
        "\n"
        "## Experience\n"
        "\n"
        "Senior Engineer\n"
        "\n"
        "## Change Log\n"
        "- Changed X\n"
        "- Changed Y\n"
        "\n"
        "## Education\n"
        "\n"
        "BS Computer Science\n"
        "<<<END>>>"
    )

    def test_cv_sections_before_change_log_heading_survive(self):
        reply = parse_reply(self._RAW)
        assert reply.kind == "final"
        assert "# Jane Smith" in reply.content
        assert "## Experience" in reply.content

    def test_change_log_heading_and_body_removed(self):
        reply = parse_reply(self._RAW)
        assert "## Change Log" not in reply.content
        assert "Changed X" not in reply.content

    def test_cv_sections_after_change_log_heading_survive(self):
        reply = parse_reply(self._RAW)
        assert "## Education" in reply.content
        assert "BS Computer Science" in reply.content
