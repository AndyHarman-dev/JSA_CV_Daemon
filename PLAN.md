# Plan: JSA Bugfix Wave 2

## Goal
Fix four regressions found during fresh-run testing after wiping the DB and output directory. These are independent, isolated bugs — each phase is scoped to a single root cause.

## Architecture reference
See ARCH.md — Python/FastAPI backend + React/Vite frontend; sentinel-based agent protocol; asyncio orchestrator; EventBus for WebSocket fan-out.

## Phases

- [x] Phase BF-4: No-sentinel fallback in `start_session` — `ClaudeCliBackend.start_session` (jsa/agents/claude_cli.py ~line 84–105) calls `parse_reply(raw)` directly with no nudge-and-retry fallback. `send_message` already has this guard (lines 155–178): on `ProtocolError("no sentinel block")` it sends a nudge prompt via `--resume` and retries once. `start_session` is missing the same treatment, so a first reply that omits the sentinel immediately raises `ProtocolError`, the orchestrator catches it, and marks the job `failed`. Fix: extract the nudge-retry logic into a private helper `_parse_with_nudge(self, session_id, raw) -> AgentReply` and call it from both `start_session` and `send_message`. The nudge message must open a `--resume <session_id>` subprocess (same as send_message does today). No DB or state-machine changes needed.

- [x] Phase BF-5: Gemini backend stuck in "running" forever — `GeminiCliBackend.start_session` spawns the `gemini` CLI as a pty, writes a large combined `system_prompt + initial_user_msg` blob to stdin, then calls `_read_until_sentinel` which blocks until `<<<END>>>` appears in pty output. The Gemini CLI is an interactive REPL; after printing its banner it sits waiting for typed input. Writing a large blob at once may cause it to echo the text back and then stall at its own prompt, never emitting the sentinel. ANSI escape codes in the pty output may also corrupt sentinel detection. This investigation+fix phase should: (1) Establish how the `gemini` CLI is actually invoked on this machine (`which gemini`, version, supported flags — does it have a `-p`/`--prompt` or `--non-interactive` mode?). (2) Check whether `_read_until_sentinel` strips ANSI codes before checking for `<<<END>>>`. (3) Determine the minimal change: if a non-interactive flag exists, switch to a subprocess approach (like ClaudeCliBackend); otherwise fix the pty write/read sequence so it correctly handles Gemini's interactive prompt loop.

- [x] Phase BF-6: No Cancel button for running jobs — `JobDetail.tsx` shows a "Dismiss" button (`showDismiss = job.state !== "approved" && job.state !== "dismissed"`), which technically appears on running jobs, but "Dismiss" is a permanent hide-forever action. There is no way to stop a running job and reset it to `pending` in one click. The existing `/api/jobs/{id}/reset` endpoint rejects running jobs (guards `state not in (failed, dismissed)`). `running → pending` is already in `ALLOWED` in `state_machine.py`. Fix: (1) Backend: add `POST /api/jobs/{id}/cancel` route in `routes_jobs.py` that accepts `running` state and calls `checkpoint(session, job, JobState.pending, new_stage=None)`; publish a `StatusChangedEvent(running → pending)` and `kick()` the orchestrator. (2) Frontend: in `JobDetail.tsx` add a "Cancel" button visible only when `job.state === "running"`; it calls a new `api.cancel(id)` fetch wrapper; on success calls `refetchAll()`. The existing "Dismiss" button continues to be shown on running jobs (for permanent dismissal).

- [x] Phase BF-7: `LogEvent` never published → LogTail always empty — `LogEvent` is defined in `jsa/events/schema.py` and the frontend's `LogTail` + `store.applyEvent` are correctly wired to render `"log"` events, but the backend never imports or publishes `LogEvent`. Fix: publish `LogEvent` at meaningful milestones in `jsa/pipeline/orchestrator.py` (job picked up, job failed to transition, `_run_one` unhandled exception) and `jsa/pipeline/stages.py` (stage starting, stage FINAL received, stage NEED_INPUT received). Also publish `ErrorEvent` in the `_run_one` exception handler after `mark_failed` succeeds so the UI shows the error text in the log. All publishes must happen after the DB commit (same as existing `StatusChangedEvent` publishes). No schema, DB, or frontend changes needed.

## Test commands

| Scope | Command |
|-------|---------|
| All backend tests | `pytest -v` |
| Skip integration | `pytest -v -m "not integration"` |
| Frontend tests | `cd frontend && npm test` |
| Server health | `curl http://localhost:8765/api/health` |

## Constraints
- All state transitions through `state_machine.transition()` — never set `Job.state` directly.
- Every DB write that changes job state: single atomic transaction via `repo.checkpoint()`.
- No mocks in tests — use `FakeAgentBackend`.
- Frontend: TypeScript strict mode, no `any`.

## Open questions
- BF-7: Does the `gemini` CLI installed on this machine support a non-interactive flag? (Coder should run `gemini --help` as the first step of that phase.)

## Change log
2026-05-27 — Rewrote plan for bugfix wave 2. Removed all completed phases (1–12, BF-1–3). Added BF-4 (start_session nudge-retry), BF-5 (LogEvent publishing), BF-6 (Cancel button for running jobs), BF-7 (Gemini pty stuck).
