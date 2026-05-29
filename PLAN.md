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

- [x] Phase BF-12: Hard-delete Cancel button at every stage — Users need a permanent "remove this job" action available regardless of pipeline stage. Currently only a soft-cancel (running → pending) and a soft-dismiss (→ dismissed, kept in DB) exist. This phase adds a hard-delete Cancel button visible at all states except `approved`.
  **Backend** (2 files):
  1. `jsa/events/schema.py`: Add `JobRemovedEvent(type="job_removed", job_id)` dataclass.
  2. `jsa/api/routes_jobs.py`: Add `DELETE /api/jobs/{job_id}` endpoint. Load job with all four relationships (`messages`, `documents`, `follow_ups`, `revision_requests`) via `selectinload` so ORM cascade fires correctly. Call `session.delete(job)` then `session.commit()`. Return `{"ok": True}`. After commit, publish `JobRemovedEvent`. Race condition for running jobs is best-effort: the in-flight orchestrator task will try to UPDATE a gone row (0 rows affected, SQLite ignores it) or get a StaleDataError caught by `_run_one`'s except handler — both are safe.
  **Frontend** (4 files):
  1. `frontend/src/types.ts`: Add `"job_removed"` to `WSEventType` union.
  2. `frontend/src/api.ts`: Add `deleteJob(id: string): Promise<{ok: boolean}>` using `DELETE /api/jobs/{id}`.
  3. `frontend/src/store.ts`: Add `removeJob(id: string)` action (removes from `jobs` map, sets `selectedId = undefined` if it matches). In `applyEvent`, add `case "job_removed": store.removeJob(e.job_id)`.
  4. `frontend/src/components/JobDetail.tsx`: Remove the existing amber "Cancel" button (running → pending). Add a red "Cancel" button visible when `job.state !== "approved"`. On click: call `window.confirm("Permanently delete this job and all its data?")`, then `api.deleteJob(job.id)`, then `refetchAll()`. Keep Dismiss button unchanged.

- [x] Phase BF-13: PDF/browser rendering sync — exported PDF styling doesn't match browser preview. Two root causes: (1) the model sometimes generates HTML (`<div align="center">`, `<strong>`, `<b>`) for styling; `marked` in the browser renders these as real HTML → centering/bold visible; `markdown-it` with `html: False` escapes them → BF-11's strip regex then removes tags AND their content styling, so the PDF loses both centering and bold. Fix: switch `markdown-it` to `html: True` and remove the HTML stripping regex — HTML passes through to WeasyPrint just like it does to the browser. (2) Even for pure `# Name` markdown, the browser preview doesn't center h1 (no `text-center` in Tailwind CSS), but the PDF does (`h1 { text-align: center; }` in styles.css). Fix: add `[&_h1]:text-center` to `MarkdownPreview.tsx`.
  **Files changed** (4):
  1. `jsa/render/weasy.py`: Remove the `re.sub(…)` HTML stripping step and the `import re`. Change `MarkdownIt("commonmark", {"html": False})` to `MarkdownIt("commonmark", {"html": True})`. HTML now passes through to WeasyPrint unchanged.
  2. `jsa/render/styles.css`: Add `[align="center"] { text-align: center; }` rule to handle deprecated HTML `align` attributes that might appear via model output.
  3. `frontend/src/components/MarkdownPreview.tsx`: Add `[&_h1]:text-center` to the h1 class string so the browser preview centers h1, matching the PDF behaviour.
  4. `tests/backend/test_bf11_html_strip.py`: Update expectations — HTML now passes through (not stripped). Flip assertions: `<div` SHOULD be in html_string; `&lt;div` should NOT be. Rename test class/file docstring to reflect pass-through semantics. Tests for autolinks, prose comparisons, and clean-markdown are unchanged.

- [x] Phase BF-14: Retry button for failed jobs — When a job reaches `failed` state, the error box in `JobDetail.tsx` shows the error message but offers no action. Add a "Retry" button inside the error box so the user can immediately re-queue the job without hunting for another control.
  **Backend:** No changes needed. `POST /api/jobs/{id}/reset` already accepts `failed` state, resets to `pending`, and kicks the orchestrator.
  **Frontend** (1 file: `frontend/src/components/JobDetail.tsx`):
  1. Add `retrying` (`boolean`) and `retryError` (`string | null`) state variables.
  2. Add `handleRetry()` async function: call `api.reset(job.id)` → on success call `refetchAll()`. On error set `retryError`.
  3. Add `showRetry = job.state === "failed"` visibility flag.
  4. Inside the error box (lines 180-185), render the error text and — when `showRetry` is true — a "Retry" button below the error text. Button label: `retrying ? "Retrying…" : "Retry"`. Style: small, outlined, white background, green or indigo border to contrast with the red error box. Disable while `retrying`.
  5. If `retryError` is non-null, show it as a small paragraph below the error box.
  **Tests** (add to `frontend/src/__tests__/JobDetail.test.tsx`):
  - Add `api.reset` to the vi.mock (return `mockResolvedValue({})` or a job shape).
  - Test: renders a "Retry" button when `job.state === "failed"` and `job.error` is set.
  - Test: does NOT render a "Retry" button when `job.state !== "failed"` (e.g. `pending`).
  - Test: clicking "Retry" calls `api.reset` with the correct job id.

- [x] Phase BF-15: Smart retry — soft reset vs nuclear reset — Two-tier retry logic for failed jobs. First Retry (retry_count==0) soft-resets only the failed stage (deletes its Messages/FollowUps, clears its session ID, rewinds job to pre-stage state). Second Retry (retry_count>0) shows a confirmation dialog in the UI; on confirm, nuclear-resets the entire job (delete all Messages, Documents, FollowUps, RevisionRequests, clear session IDs, reset to pending). Requires: Job.retry_count DB column + migration, preserve current_stage on mark_failed, failed→cv_done transition, soft_reset_job/nuclear_reset_job repo helpers, reset endpoint branching, _job_to_dict including retry_count, _handle_final resetting retry_count to 0 on success, upsert_job using nuclear semantics for failed jobs, and a frontend confirmation modal for the nuclear path.

- [~] Phase BF-16: Change Log section appearing inside rendered CV — `PROMPT_CDADJUST.md` step 6 instructs the model to include the Change Log **inside** the `<<<FINAL>>>` block. `parse_reply` stores the entire FINAL content as the document markdown, so the Change Log ends up rendered in the PDF/browser preview. The issue is most consistent with Gemini CLI (which follows the prompt instruction literally).
  **Root cause:** The prompt says "inside the `<<<FINAL>>>` block" — the model obeys.
  **Fix — two layers:**
  1. **Prompt** (`jsa/prompts/PROMPT_CDADJUST.md`): Change step 6 so the Change Log is written as conversational reply text **before** the `<<<FINAL>>>` sentinel, not inside it. Add an explicit sentence: "Do NOT include the Change Log inside the `<<<FINAL>>>` block — the FINAL block must contain only the clean CV Markdown." Update the Output format reminder to reflect this.
  2. **Code safety net** (`jsa/agents/protocol.py`): After extracting `content` from a FINAL block, strip any embedded Change Log — both `<change_log>…</change_log>` XML wrapper and any trailing `## Change Log` / `### Change Log` Markdown section — using a small `_strip_change_log(content)` helper. This catches models that ignore the updated prompt.
  **Tests** (`tests/backend/test_bf16_change_log_strip.py`):
  - `parse_reply` returns clean content when FINAL has an XML `<change_log>` block.
  - `parse_reply` returns clean content when FINAL has a `## Change Log` Markdown section.
  - `parse_reply` is unchanged when FINAL has no Change Log at all.
  - Prompt test: confirm the word "before" (or equivalent) is present in the updated step 6 instruction and "inside the `<<<FINAL>>>` block" is absent from the Change Log instruction.

## Change log
2026-05-27 — Rewrote plan for bugfix wave 2. Removed all completed phases (1–12, BF-1–3). Added BF-4 (start_session nudge-retry), BF-5 (LogEvent publishing), BF-6 (Cancel button for running jobs), BF-7 (Gemini pty stuck).
2026-05-28 — Added BF-8: duplicate open FollowUp UNIQUE constraint crash on multi-turn NEED_INPUT and failed-job reset.
2026-05-28 — Added BF-9: CV/CL revision resumes wrong CLI session causing cover-letter content to appear in CV section.
2026-05-28 — Added BF-10: Revision stages have no awaiting_input resume path — infinite NEED_INPUT loop.
2026-05-28 — Added BF-11: HTML tags visible as literal text in exported CV PDF — prompt instructed AI to use HTML divs; renderer had html:False.
2026-05-28 — Added BF-12: Hard-delete Cancel button at every stage.
2026-05-28 — Added BF-13: PDF/browser rendering sync — html:True in markdown-it passes HTML through to WeasyPrint; add text-center to browser h1.
2026-05-28 — Added BF-14: Retry button for failed jobs — frontend-only; backend reset endpoint already handles failed→pending.
2026-05-29 — Added BF-16: Change Log section appearing inside rendered CV — prompt told model to include Change Log in FINAL block; fix: move it before FINAL in prompt + strip safety net in protocol.py.
