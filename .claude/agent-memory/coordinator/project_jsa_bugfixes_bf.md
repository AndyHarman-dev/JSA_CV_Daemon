---
name: project-jsa-bugfixes-bf
description: BF-1/BF-2/BF-3 bugfix phases — dismiss state, JD tab, timeout, prompt fixes
metadata:
  type: project
---

## BF-1 Backend Bugfixes (complete — 2026-05-27)

- **`dismissed` JobState**: Added as terminal state (not approved-terminal — can be re-queued via reset). Transitions: all non-terminal/non-approved → dismissed; dismissed → pending. `mark_failed` guards against both `approved` AND `dismissed` to prevent race when pipeline task outlives the dismiss action.
- **JD in API**: `_job_to_dict` now always includes `jd` field (both summary and full endpoint). `JobDTO.jd: string` in frontend types.
- **Agent timeout**: `agent_timeout: float = 300.0` in `config.py` (env: `JSA_AGENT_TIMEOUT`). `server.py` passes `timeout=settings.agent_timeout` to ALL CLI backends (was missing for claude-cli/gemini-cli; anthropic already had separate `anthropic_timeout`).

**Why:** Claude CLI was timing out at 120s on complex CV tasks. Dismissed state needed because intel brief can tell user a job is unsuitable before pipeline completes.

## BF-2 Frontend Bugfixes (complete — 2026-05-27)

- **Dismiss/Re-queue UI**: Dismiss button (not shown for approved/dismissed); Re-queue for dismissed only. Both have loading spinner + error display. `api.dismiss()` added.
- **JD collapsible**: Toggle section in `JobDetail` using `job.jd`. `▶/▼` indicator, `<pre>` element, max-h-64 scroll.
- **ReviewPane**: `useEffect([jobId])` — do NOT add `state` to dep array. It would cause a "Loading…" flash on approve. State change on revision is handled by unmount/remount of ReviewPane when job goes running→review.

**Why:** Revert from `[jobId, state]` dep — the revision case uses unmount/remount anyway (JobDetail conditionally renders ReviewPane only for review/approved).

## BF-3 Prompt Fixes (complete — 2026-05-27)

- **CVL_PROMPT.md**: STEP 4 delivers draft in plain text + ends with `<<<NEED_INPUT>>>` asking for changes or "finalize". STEP 5 sub-case B: on approval, copy full letter into `<<<FINAL>>>`. HARD RULE added: no "see above" in `<<<FINAL>>>`.
- **PROMPT_CDADJUST.md**: Step 3 now instructs Markdown output (not docx/docx.js). Format preservation rules added: `<div align="center">` for centered elements; ATS rules apply to structural elements only (not visual styling). HARD RULE: full Markdown CV must be in `<<<FINAL>>>`, not a file reference.

**Why:** The `.docx`/`docx.js` instruction was impossible for Claude CLI and caused format improvisation. "Override unconditionally" wording caused misalignment of centered names, etc.
