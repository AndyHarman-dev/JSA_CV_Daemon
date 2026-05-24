---
name: project-jsa-context
description: Core context for the JSA (Job Search Assistant) project — stack, key decisions, and non-obvious conventions
metadata:
  type: project
---

JSA is a CLI-launched local web app (FastAPI + React/Vite + SQLite) that automates tailored job application documents (CV adjust + cover letter) via AI agents. Up to 5 jobs run in parallel async workers; parked jobs (awaiting user follow-up) free their slot immediately.

**Why:** User wants to apply to many jobs without equal effort everywhere — tier system (A/B/C) modulates AI research depth built into the CV-adjust agent prompt.

**Key non-obvious decisions:**
- Sentinel protocol: agents MUST end every reply with `<<<NEED_INPUT>>>...<<<END>>>` or `<<<FINAL>>>...<<<END>>>`. Prompt files must enforce this.
- Park-as-task-exit: parked jobs exit their worker (slot freed). Resumed via fresh task + `restore_session`.
- Session restore is native (Anthropic = full messages array; Claude CLI = `--resume <id>`), NOT user-turn replay — replay silently diverges.
- Revision flow stays in `review` state via `current_stage ∈ {revising_cv, revising_cl}`.
- PDF rendered post-approval only (never speculatively).
- All state transitions via `state_machine.transition()` — never set Job.state directly.
- All checkpoints in single DB transaction via `repo.checkpoint()`.

**How to apply:** Any architectural or implementation decision should respect these invariants. Agent prompts must always include sentinel grammar.
