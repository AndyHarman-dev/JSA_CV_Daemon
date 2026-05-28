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

- [x] Phase BF-8: Duplicate open FollowUp → UNIQUE constraint crash on re-run — Two bugs in `jsa/db/repo.py` combine to crash jobs at NEED_INPUT when a job has been processed before.
  **Bug 1** (`list_runnable_jobs`): condition 3 only checks that an answered FollowUp exists for `current_stage`, but does NOT check that no open (unanswered) FollowUp also exists. When a multi-turn NEED_INPUT cycle occurs (user answers → agent asks again → second open FollowUp committed), the old answered FollowUp makes the job appear runnable immediately, the orchestrator re-dispatches it, the agent returns NEED_INPUT again, and `checkpoint` tries to INSERT a third open FollowUp — blocked by the partial unique index.
  **Bug 2** (`checkpoint` insert path): When a job is reset `failed → pending` (by `upsert_job` on startup), stale open FollowUps are left in the DB. The next run's NEED_INPUT checkpoint tries to INSERT a new open FollowUp → same UNIQUE constraint failure.
  **Fix 1** — `list_runnable_jobs` condition 3: add a `no_open_followup` negation subquery on `(job_id, current_stage, answered_at IS NULL)` AND'd with `answered_followup`.
  **Fix 2** — `checkpoint` insert-FollowUp path (the `else` branch at ~line 223): before `session.add(fu)`, execute `DELETE FROM follow_ups WHERE job_id=? AND stage=? AND answered_at IS NULL` to purge any stale open FollowUp atomically within the same transaction.
  Both changes are confined to `jsa/db/repo.py`.

- [x] Phase BF-9:
- [x] Phase BF-10: Revision stages (`revising_cv`, `revising_cl`) have no `awaiting_input` resume path. When the agent asks a follow-up mid-revision and the user answers, `run_stage` re-runs `revising_cl` but always takes the "fresh revision" path — it re-loads the original stage history, fetches the (still unconsumed) `RevisionRequest` instruction, and re-sends the original instruction. The agent sees the same instruction, produces another NEED_INPUT "here's the draft, finalize?" — infinite loop.
  **Root cause**: Lines 71-91 of `stages.py` have NO discriminator for fresh-vs-resume, unlike the `cv_adjust`/`cover_letter` branch (lines 92-116) which checks `_load_history(session, job.id, stage)`. Because NEED_INPUT messages during a revision are stored with `message_stage = job.current_stage = Stage.revising_cl`, checking `_load_history(session, job.id, stage)` (where `stage = Stage.revising_cl`) correctly distinguishes fresh (empty) from resume (non-empty).
  **Fix** — single-file change to `stages.py`: Split the revision branch into fresh vs resume. Fresh: existing logic (get revision instruction, send it). Resume: get the user's latest answer via `_get_latest_answer(session, job.id, stage)`, restore session with combined history (`original_history + revision_turns`) for API backends (CLI backends ignore history), send the answer. Exact structure:
  ```python
  revision_turns = await _load_history(session, job.id, stage)
  if revision_turns:
      # Resume path: answer a follow-up within the revision
      answer_text = await _get_latest_answer(session, job.id, stage)
      original_history = await _load_history(session, job.id, original_stage)
      combined_history = original_history + revision_turns
      handle = await backend.restore_session(system_prompt, combined_history, revision_session_id)
      reply = await backend.send_message(handle, answer_text)
      accumulated_messages = [{"role": "user", "content": answer_text}, {"role": "assistant", "content": reply.raw}]
  else:
      # Fresh revision path: existing logic
      ...
  ``` CV revision resumes wrong CLI session — critical data-corruption bug. After both stages complete, `job.session_external_id` holds the **cover_letter** session UUID (the last one set). When `revising_cv` runs, `stages.py` calls `backend.restore_session(history, job.session_external_id)` — both `ClaudeCliBackend` and `GeminiCliBackend` ignore `history` and pass `external_id` directly to `claude --resume` / `gemini --resume`. Result: the AI continues the cover-letter conversation and produces cover-letter text, which gets stored as the `cv_adjust` document. The user sees cover-letter content in the CV section.
  **Fix**: Add `cv_session_id` and `cl_session_id` columns to `Job` (both `VARCHAR(128) nullable`). In `stages.py`, after a handle is obtained for `cv_adjust`, set `job.cv_session_id = handle.external_id`; likewise `cl_session_id` for `cover_letter`. In the revision path, pass `job.cv_session_id` for `revising_cv` and `job.cl_session_id` for `revising_cl` to `restore_session` instead of `job.session_external_id`. `session_external_id` continues to be updated as before (needed for `awaiting_input` resume). DB migration: in `init_db` (engine.py), after `create_all`, run `ALTER TABLE jobs ADD COLUMN cv_session_id VARCHAR(128)` and `ALTER TABLE jobs ADD COLUMN cl_session_id VARCHAR(128)` wrapped in try/except (SQLite ignores "duplicate column" errors). Legacy jobs in `review` state before this fix will have both columns NULL → revision will hit a backend `RuntimeError` (already raises "external_id is None") — this is acceptable and better than silent corruption.

- [x] Phase BF-11: HTML tags visible as literal text in exported CV PDF — `PROMPT_CDADJUST.md` (line 100) instructs the AI to use `<div align="center">…</div>` for the name/contact block. The renderer (`weasy.py`) creates `MarkdownIt` with `{"html": False}`, so inline HTML is escaped and appears as literal text (e.g. `<div align="center">Name</div>`) in the PDF rather than being rendered or hidden.
  **Fix** — three files:
  1. `jsa/prompts/PROMPT_CDADJUST.md`: Replace the `<div align="center">…</div>` centering instruction with "use a top-level Markdown heading (`# Name`) — the PDF stylesheet centres `h1` automatically. Do NOT use raw HTML tags (`<div>`, `<p>`, `<span>`, etc.) anywhere in the output."
  2. `jsa/render/styles.css`: Add `text-align: center` to the existing `h1` rule (currently `h1 { font-size: 18pt; margin-bottom: 4pt; }`) so the name heading is visually centred in the PDF.
  3. `jsa/render/weasy.py`: Before calling `md.render(markdown)`, strip any raw HTML tags from the markdown string with `import re` and `clean_markdown = re.sub(r'<[^>]+>', '', markdown)`. This sanitises existing DB documents that already contain HTML tags. Pass `clean_markdown` to `md.render()` instead of the raw `markdown`. The `{"html": False}` setting can remain unchanged.

- [~] Phase BF-12: Hard-delete Cancel button at every stage — Users need a permanent "remove this job" action available regardless of pipeline stage. Currently only a soft-cancel (running → pending) and a soft-dismiss (→ dismissed, kept in DB) exist. This phase adds a hard-delete Cancel button visible at all states except `approved`.
  **Backend** (2 files):
  1. `jsa/events/schema.py`: Add `JobRemovedEvent(type="job_removed", job_id)` dataclass.
  2. `jsa/api/routes_jobs.py`: Add `DELETE /api/jobs/{job_id}` endpoint. Load job with all four relationships (`messages`, `documents`, `follow_ups`, `revision_requests`) via `selectinload` so ORM cascade fires correctly. Call `session.delete(job)` then `session.commit()`. Return `{"ok": True}`. After commit, publish `JobRemovedEvent`. Race condition for running jobs is best-effort: the in-flight orchestrator task will try to UPDATE a gone row (0 rows affected, SQLite ignores it) or get a StaleDataError caught by `_run_one`'s except handler — both are safe.
  **Frontend** (4 files):
  1. `frontend/src/types.ts`: Add `"job_removed"` to `WSEventType` union.
  2. `frontend/src/api.ts`: Add `deleteJob(id: string): Promise<{ok: boolean}>` using `DELETE /api/jobs/{id}`.
  3. `frontend/src/store.ts`: Add `removeJob(id: string)` action (removes from `jobs` map, sets `selectedId = undefined` if it matches). In `applyEvent`, add `case "job_removed": store.removeJob(e.job_id)`.
  4. `frontend/src/components/JobDetail.tsx`: Remove the existing amber "Cancel" button (running → pending). Add a red "Cancel" button visible when `job.state !== "approved"`. On click: call `window.confirm("Permanently delete this job and all its data?")`, then `api.deleteJob(job.id)`, then `refetchAll()`. Keep Dismiss button unchanged.

## Change log
2026-05-27 — Rewrote plan for bugfix wave 2. Removed all completed phases (1–12, BF-1–3). Added BF-4 (start_session nudge-retry), BF-5 (LogEvent publishing), BF-6 (Cancel button for running jobs), BF-7 (Gemini pty stuck).
2026-05-28 — Added BF-8: duplicate open FollowUp UNIQUE constraint crash on multi-turn NEED_INPUT and failed-job reset.
2026-05-28 — Added BF-9: CV/CL revision resumes wrong CLI session causing cover-letter content to appear in CV section.
2026-05-28 — Added BF-10: Revision stages have no awaiting_input resume path — infinite NEED_INPUT loop.
2026-05-28 — Added BF-11: HTML tags visible as literal text in exported CV PDF — prompt instructed AI to use HTML divs; renderer had html:False.
2026-05-28 — Added BF-12: Hard-delete Cancel button at every stage.
