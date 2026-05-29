---
name: project-jsa-phase-bf15
description: BF-15 smart retry — soft reset vs nuclear reset; key gotchas and design decisions
metadata:
  type: project
---

## Phase BF-15: Smart Retry (soft vs nuclear)

**Why:** Retry button was broken for session-loss failures (Gemini/Claude CLI session deleted). `reset_job` reset to `pending` but kept Message rows — orchestrator then tried to resume the dead session and reproduced the same error.

**Design:**
- `retry_count == 0` → soft reset: delete only failed stage's Messages/FollowUps, clear its session ID, rewind to `cv_done` (if cover_letter/revising_cl failed) or `pending` (if cv_adjust/revising_cv/None failed). Set `retry_count = 1`.
- `retry_count > 0` → frontend shows inline confirmation modal. On confirm: nuclear reset — delete ALL Messages, Documents, FollowUps, RevisionRequests; reset to `pending`; `retry_count = 0`.

**Key implementation details:**
- `mark_failed` preserves `current_stage` after `transition(job, failed, None)` by saving and restoring it directly (documented ARCH.md exception — transition forces None, we restore after).
- `failed → cv_done` added to `ALLOWED` in state_machine.py for soft-reset of cover_letter failures.
- `retry_count = 0` is reset in `_handle_final` BEFORE the `checkpoint()` call so it persists atomically.
- `upsert_job` for failed jobs now uses nuclear semantics (CSV re-import = always start clean).
- Dismissed jobs still go through the existing `checkpoint → pending` path in `reset_job`; the smart branching only applies to `failed` state.

**Frontend confirmation modal:**
- `showNuclearConfirm` state in JobDetail.tsx.
- `handleRetry()` branches on `job.retry_count > 0` to show modal vs act immediately.
- `handleNuclearConfirm()` does the actual API call on yes-click.
- Modal is inline JSX (no separate component), red bordered, with "Yes, restart from scratch" and "Cancel" buttons.

**`retry_count` in API:** Included in base `_job_to_dict` (not gated on `full=True`) so frontend always has it.

**How to apply:** Any future "restart this stage" feature should follow the same pattern: delete Message rows for the target stage before resetting, so `_load_history` returns empty and starts fresh instead of trying to resume a dead session.
