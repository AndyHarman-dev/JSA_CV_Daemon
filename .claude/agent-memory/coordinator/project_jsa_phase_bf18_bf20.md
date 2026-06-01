---
name: project-jsa-bf18-bf20
description: Gotchas and decisions from BF-18 (AgentLimitReached) and BF-20 (DocxRenderer + docx_path)
metadata:
  type: project
---

## BF-18 — AgentLimitReached

- `AgentLimitReached(RuntimeError)` lives in `agents/base.py` alongside `AgentTimeout`.
- Keyword check in `_parse_with_nudge` is case-insensitive on `raw.lower()`, only fires when `ProtocolError("no sentinel block")` is raised first — messages with a valid sentinel block never reach this path even if they contain limit keywords.
- Anthropic backend catches `anthropic.RateLimitError` (HTTP 429) in `_call_api` — import is already local inside the method, so `AgentLimitReached` must be imported at module level separately.
- Orchestrator `except AgentLimitReached` branch uses `logger.warning` (not `logger.exception`) and the exact message `"Backend limit reached — switch backends or wait for quota reset"`.
- Discrepancy in spec: ARCH.md prose says "RuntimeError to match AgentTimeout" but AgentTimeout actually extends Exception. `AgentLimitReached` was implemented as `RuntimeError` as the spec code fragment says — note this asymmetry if you ever consolidate.

## BF-20 — DocxRenderer

- `python-docx` was already present in pyproject.toml (used by cv_loader for DOCX input) — no new dep needed.
- Contact-line detection bug: `expect_contact` flag stays True after H1; if the next non-blank line starts with `#` (H2, H3, …), the guard `not stripped.startswith("#")` must skip contact treatment AND reset the flag. Two separate guards needed: one to skip the contact block, one (lines 137-138) to clear the flag.
- Unused `import re` crept in — removed in the cleanup commit.
- Bullet color was guarded by `if run.font.color.type is not None` (inconsistent); standardised to unconditional assignment matching all other elements.
- `docx_path` column is infrastructure-only in BF-20 — populated by the export endpoint in BF-21.

**Why:** Both phases ran in parallel (BF-18 on main track, BF-20 as background sub-agent). Git was broken mid-session (Xcode license) so tags had to be created retroactively pointing to pre-BF-18 commit `1f261c8`.
