---
name: project-jsa-phase-bf14
description: BF-14 Retry button for failed jobs — frontend-only; test commit oversight pattern
metadata:
  type: project
---

## BF-14: Retry button for failed jobs

**Fact:** BF-14 is a frontend-only change. `POST /api/jobs/{id}/reset` already accepted `failed` state before this phase — no backend changes were needed.

**Why:** The existing "Re-queue" button only showed for `dismissed` state; `failed` jobs had an error box with no action. The Retry button was added inside the error box (`{job.error && ...}`) guarded by `showRetry = job.state === "failed"`. This means a failed job with `error: null` won't show the button — intentional, matches test spec.

**Gotcha — commit oversight:** The Tester added test changes that passed locally but were left **uncommitted** in the working tree. The Reviewer caught this as a blocker. Always verify `git status` shows test files staged before declaring a phase done. The Coordinator committed the test file as a follow-up commit.

**`handleRetry` = `handleRequeue` duplication:** Both call `api.reset(job.id)` + `refetchAll()`. Reviewer flagged this as a code quality concern. Acceptable for now — they're separate state vars (`retrying`/`retryError` vs `requeuing`/`requeueError`) so future divergence is easy. Could be refactored to a shared helper if a third similar handler is added.

**How to apply:** For future frontend-only phases, double-check that test file modifications are staged and committed before invoking Reviewer.
