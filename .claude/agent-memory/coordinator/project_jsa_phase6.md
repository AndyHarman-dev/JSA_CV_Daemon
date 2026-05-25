---
name: project-jsa-phase6
description: Phase 6 gotchas — cover_letter split-commit, orchestrator task GC, fresh/resume discriminator
metadata:
  type: project
---

cover_letter finalization must use a single checkpoint(→ review) call — do NOT do cl_done commit then review commit. The state machine allows running→review directly. cl_done is only a crash-recovery landing state (startup sweep), not an intermediate observable state in normal flow.

**Why:** Two commits creates a crash-recovery hole: a crash between commit 1 (cl_done) and commit 2 (review) strands the job permanently — the startup sweep only reverts running→prior, not cl_done→review.

**How to apply:** Any future stage that needs to "pass through" a transient state should go directly to the final state in one checkpoint. The startup sweep handles the transient-state question via document existence, not via the last committed state.

Orchestrator: asyncio.create_task() results must be held in self._tasks set (discard on done callback) to prevent GC of in-flight tasks.

Fresh vs resume in run_stage is determined by whether Message rows exist for (job_id, stage) — NOT by job.state, because the orchestrator pre-transitions the job to running before spawning the task.
