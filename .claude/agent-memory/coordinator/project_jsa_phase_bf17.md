---
name: project-jsa-phase-bf17
description: BF-17 CV format rules — header block spec, unconditional separators, CSS tightening for ≤2 pages
metadata:
  type: project
---

Phase BF-17 (complete) — CV format rules for ATS-friendly, ≤2-page output.

**Why:** Model was producing a stray "profession title" line between name and contacts, no `---` separators between sections, and generous CSS settings pushing CVs to 3–4 pages.

**Changes made:**
- `jsa/prompts/PROMPT_CDADJUST.md`: mandated exact two-line header (# Full Name / contact paragraph, no title), unconditional `---` before every section heading, compactness rules (no blank lines between bullets or between date line and bullet list).
- `jsa/render/styles.css`: `@page margin 1in→0.75in`, `font-size 11pt→10.5pt`, `line-height 1.5→1.3`, `h2 margin-top 12pt→8pt`, `h3 margin-top 8pt→4pt`, `p/ul/ol margin 4pt→2pt`, `li margin 2pt→1pt`, `hr margin 12pt→6pt`. Yields ~17 extra lines per 2 pages.

**Review note:** Tests cover 6 of 9 CSS changes; `p`, `ul/ol`, and `li` margin assertions were omitted. Not a blocker but if CSS spacing regresses in those three rules, tests won't catch it.

**How to apply:** If CV spacing is again reported as too large, check these CSS values first before re-prompting. If model still emits a title line, check that the "No profession title" prompt instruction is still present.
