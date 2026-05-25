---
name: project-jsa-phase2-3
description: Phase 2–3 gotchas — transition() new_stage requirement, checkpoint atomicity order, csv_loader intentional behavior
metadata:
  type: project
---

## transition() requires new_stage when entering running or awaiting_input

`state_machine.transition(job, new_state, new_stage=None)` raises `InvalidTransition` if you try to enter `running` or `awaiting_input` without supplying a `new_stage`. This was a Phase 2 fix.

**Why:** `running` and `awaiting_input` are meaningless without knowing which stage is active — the orchestrator and resume path both depend on `current_stage`.

**How to apply:** Phase 8 API routes and any code that triggers state transitions must always pass `new_stage` when transitioning to `running` or `awaiting_input`. Never call `transition(job, JobState.running)` alone.

---

## repo.checkpoint() calls transition() BEFORE staging rows

`repo.checkpoint()` calls `transition(job, new_state, new_stage)` as its first step, before queuing any Message/Document/FollowUp rows. This means:
- If the transition is invalid, the entire checkpoint raises and nothing is written — atomicity is maintained.
- The guard is structural (fail-fast), not a post-hoc validation.

**How to apply:** Never pre-call `transition()` before `checkpoint()` — `checkpoint` handles it internally. Doing so would call `transition` twice and the second call would see the already-mutated state.

---

## mark_failed() guards against the approved terminal state

`repo.mark_failed(session, job_id, error)` checks `job.state != approved` before marking failed. This prevents the orchestrator's catch-all exception handler from overwriting a successfully-approved job.

**How to apply:** No action needed — the guard is in place. But be aware that if you see a job stuck in `approved` state that shouldn't be, `mark_failed` won't help; you'd need a direct DB update.

---

## csv_loader intentionally does NOT skip partial-blank rows

Rows where most fields are whitespace but tier is valid are NOT skipped by `csv_loader`. This was a deliberate choice per spec — only invalid tier values are soft-skipped.

**How to apply:** If stricter blank-row validation is needed before Phase 8 goes live, add a guard in `csv_loader.py` at that time and document it in the change log. Don't assume blank rows are filtered.
