---
name: project-jsa-phase-bf16
description: BF-16 gotchas — Change Log in rendered CV; prompt placement vs code strip; regex scope hazard
metadata:
  type: project
---

## BF-16: Change Log appearing in rendered CV

**Root cause:** `PROMPT_CDADJUST.md` step 6 explicitly told the model to include the Change Log INSIDE the `<<<FINAL>>>` block. `parse_reply` stores the full FINAL content as document markdown, so the Change Log ends up in the PDF/preview. Gemini follows prompt instructions especially literally — Claude may be more lenient.

**Fix — two layers:**
1. **Prompt** (`jsa/prompts/PROMPT_CDADJUST.md` step 6): Moved Change Log instruction to BEFORE `<<<FINAL>>>`. Added explicit prohibition: "Do NOT include the Change Log inside the `<<<FINAL>>>` block." Added illustrative example showing the expected output order.
2. **Code safety net** (`jsa/agents/protocol.py`): `_strip_change_log()` called on FINAL content only. Two regexes — XML wrapper and Markdown heading form.

**Why:** The prompt is the primary fix; the code strip is defense-in-depth for when models ignore the instruction.

**Regex scope hazard caught in review:**
- Greedy `^#{2,3}\s+Change\s+Log.*` with `re.DOTALL` would consume everything from the heading to end-of-string, silently deleting all CV sections that follow a mid-document Change Log heading.
- Fix: `^#{2,3}\s+Change\s+Log\b[^\n]*(?:\n(?!#{1,3}\s)[^\n]*)*` — stops at the next Markdown heading. No `re.DOTALL` needed; uses `[^\n]*` instead.

**XML whitespace hazard:**
- `\s*<change_log>...\s*` collapses surrounding blank lines (sections run together).
- Fix: `\n?[ \t]*<change_log>...\n?` — at most one newline consumed on each side.

**How to apply:** When writing regex-based content strippers, use non-DOTALL patterns with explicit line anchors and lookaheads to limit scope, and use `[ \t]*\n?` not `\s*` to preserve paragraph boundaries.
