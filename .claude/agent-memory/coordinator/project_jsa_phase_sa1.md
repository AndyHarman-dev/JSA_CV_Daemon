---
name: project-jsa-phase-sa1
description: Phase SA-1 gotchas and decisions for the research subagent split
metadata:
  type: project
---

Phase SA-1 (research subagent split) complete as of 2026-05-31.

**Why:** Both stage prompts triggered interactive permission dialogs when browsing. Splitting into `.claude/agents/` subagents pre-authorizes `WebSearch` and `WebFetch` in frontmatter — no dialog.

**Key decisions:**
- `run_research` is on `ClaudeCliBackend` only (NOT on the `AgentBackend` ABC). `isinstance`-gate in `_gather_research` routes all other backends to the NONE placeholder.
- `cwd=_PROJECT_ROOT` passed to `_run` ONLY from `run_research`. Main-stage calls keep `cwd=None` to avoid this repo's `CLAUDE.md` bleeding into CV/CL context.
- Research agents emit plain text, NO sentinel grammar — deliberate exception to the mandate.
- Brief injected into the fresh `initial_user_msg` → persists as a `Message` row → replays verbatim on resume/crash → research runs at most once per stage.
- `_gather_research` fires ONLY in the fresh-session branch (`elif stage in (cv_adjust, cover_letter)` + `else:` block). Never on resume, never on `revising_*`.

**Gotchas:**
- Agent files must use bare model alias (`model: sonnet`) not full IDs (`model: claude-sonnet-4-5`) — commit `abb3840` had the wrong value; fixed in `f0386ab`.
- Tester's test file was committed in a separate fix commit (`f0386ab`) — the Coder commit (`abb3840`) left it untracked. Always verify `git status` before declaring phase done ([[feedback-test-commit-oversight]]).
- `_research_spec` originally had a silent `else` catch-all; changed to explicit `elif stage == Stage.cover_letter` + `raise ValueError` so revision-stage misuse is caught loudly.
- The broad `except Exception` in `_gather_research` is intentional (best-effort); but it means a 100%-broken research pipeline still passes all mocked tests. A manual smoke test with a real `jsa` invocation is the only way to confirm the feature actually works end-to-end.

**How to apply:** SA-2 (prompt edits) can proceed — the injection infrastructure is in place. PROMPT_CDADJUST.md Phase 1 and CVL_PROMPT.md Step 1 need updating to consume the brief blocks instead of browsing inline.
