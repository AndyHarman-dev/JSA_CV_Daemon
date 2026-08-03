---
status: Done
---

# JSA Reliability Hardening — Batch (parallel worktrees)

## Context

The `JSA_reliability_review.md` audit found the pipeline's happy path is well-engineered,
but it is fragile on the three axes that decide whether "5 in parallel across 100 jobs" is
*reliable* rather than *usually fine*: **behavior under real parallel load, behavior on
oversized input, and token economics.** None of the 11 findings are logic bugs in the state
machine / checkpoint / stale-guard core (which is good and stays untouched) — they are
missing hardening at the edges.

This batch decomposes all 11 findings into **11 independent work units**, each implemented
in its own git worktree. Per the user: **no PR/MR**; each worker **commits locally** to its
worktree branch and stops. Merging is deferred and manual, so textual conflicts on shared
hot files are acceptable — units are decomposed for **semantic** independence, which holds
across all 11.

## Research summary (verified against current source, not just the review)

- **DB** (`jsa/db/engine.py:15,68`): `create_async_engine(url, echo=False)` — no `connect_args`,
  no PRAGMA, no event listener → `journal_mode=delete`, `busy_timeout=0`. SQLITE_BUSY-prone.
- **Orchestrator** (`jsa/pipeline/orchestrator.py`): `run()` loop; `sem.acquire()` (191) and
  `_next_stage_for(job)` (195) sit **outside** the `try:` (196). `_next_stage_for` (lines 33–55,
  *in orchestrator.py*, not state_machine.py) raises `ValueError` for `awaiting_input`/`review`
  with `current_stage is None`. `_run_one`'s `finally` (358) already `sem.release()`. `max_parallel`
  is an `__init__` default of 5 (102/107), never Settings-driven; `server.py` (113–120) omits it.
- **Server** (`jsa/server.py:125`): `asyncio.create_task(orchestrator.run())` — **handle not stored**.
  `_shutdown` (133–140) only sets `_stopping`, no drain. `recovery_sweep` runs **only** at CLI
  preflight (`jsa/cli.py:274`), never at server startup.
- **CSV** (`jsa/ingest/csv_loader.py`): `csv.field_size_limit()` never raised (default 128 KiB);
  row loop (line 68) has **no per-row try/except**; oversized field → `_csv.Error` propagates out
  of `load_csv` → out of `_preflight` (`jsa/cli.py:264`, unwrapped) → **aborts startup**. An
  `errors: list[str]` soft-skip channel already exists (line 66).
- **Anthropic backend** (`jsa/agents/anthropic_api.py`): fresh `AsyncAnthropic()` per call (109);
  `max_tokens=8192` hardcoded (114); `system`+`messages` sent as plain strings, **no `cache_control`**
  (111–119); `response.content[0].text` unguarded (130); only `RateLimitError`/`TimeoutError` mapped
  (120–127). No `max_tokens` field in `jsa/config.py`.
- **CLI backend** (`jsa/agents/claude_cli.py:166–174`): prompt passed as `-p <argv>` via
  `run_killable` → `create_subprocess_exec` (in `jsa/agents/_subprocess.py`) → `ARG_MAX` ceiling.
- **API routes** (`jsa/api/routes_jobs.py`): `AnswerBody.text` / `ReviseBody.text` bare `str`, no
  `max_length` (99–106); `_job_to_dict` sets `"jd": job.jd` unconditionally (68) → `/api/jobs` list
  ships every JD.
- **Backend switch** (`jsa/db/repo.py::backend_switch_reset` 271–372): deletes stage `Message` rows,
  rewinds to stage start; `_handle_limit_reached` (orchestrator 362–432) drives it. No rate-limit
  backoff — 5 workers call the provider independently.
- **Events** (`jsa/events/bus.py:18`): `asyncio.Queue()` no `maxsize`; `publish` `await q.put` (14).
  `ws.py` does unsubscribe on disconnect (46) — bounded except for a slow-but-connected client.
- **Fit gate** (`jsa/pipeline/stages.py::_run_fit_assessment` 733–796, `_build_fit_user_msg` 680–695):
  always-on first stage; embeds full `cv_to_markdown(base_structure)` per job; no disable flag,
  no CV-prefix cache. Config pattern to mirror for a new global flag: `jsa/store/preferences.py`
  (JSON re-read at use-time) or a `Settings` field (`jsa/config.py`, env prefix `JSA_`).
- **Stage budget**: timeouts are per agent call only (`agent_timeout=600`, `anthropic_timeout=180`,
  research `300`); a stage can chain research + main + self-heals with no overall wall-clock cap.

## Work units (11 — one worktree each)

| # | Title | Files (primary) | Change | Sev |
|---|-------|-----------------|--------|-----|
| 1 | **DB durability (WAL + busy_timeout)** | `jsa/db/engine.py` | Add a `create_async_engine` connect-event listener enabling `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA synchronous=NORMAL` on every aiosqlite connection (both `make_engine` and `create_engine`). | P0 |
| 2 | **Dispatcher liveness** | `jsa/pipeline/orchestrator.py`, `jsa/server.py` | Wrap the `run()` while-body in `try/except Exception: log+continue`; move `sem.acquire()`+`_next_stage_for` inside the guard and release the slot **only** on the not-spawned path (`_run_one`'s `finally` already releases). Store the `run()` task handle in `server.py`; attach a done-callback that logs and restarts on unexpected exit. Call `recovery_sweep` at server startup (before the loop; idempotent w/ CLI preflight). | P0 |
| 3 | **CSV oversized-field resilience** | `jsa/ingest/csv_loader.py`, `jsa/cli.py` | `csv.field_size_limit(sys.maxsize)` (or a chosen cap); wrap per-row parsing so a too-large/broken row becomes a soft-skip appended to the existing `errors` channel instead of aborting. Belt-and-braces: wrap the `load_csv` call in `_preflight` so ingest failure degrades gracefully, never kills startup. | P0 |
| 4 | **Anthropic backend hardening** | `jsa/agents/anthropic_api.py`, `jsa/config.py` | Restructure `system`/stable-prefix into content blocks with `cache_control:{type:"ephemeral"}` breakpoints (prompt caching); add a configurable `max_tokens` `Settings` field (default e.g. 16384) threaded via `backend_for` kwargs; detect `stop_reason=="max_tokens"` → surface "output truncated" not a schema error; guard `response.content` before `[0].text`; reuse one `AsyncAnthropic` client; map `APITimeoutError`/`APIConnectionError`/`OverloadedError`/`BadRequestError` to friendly errors; validate `ANTHROPIC_API_KEY` presence with a clear message. | P1 |
| 5 | **CLI backend stdin payload** | `jsa/agents/claude_cli.py`, `jsa/agents/_subprocess.py` | Feed the user prompt to `claude -p` via **stdin** instead of an argv element (drop `stdin=DEVNULL` on the payload path in `run_killable`), removing the `ARG_MAX` ceiling for large structured prompts. | P1 |
| 6 | **Backend-switch history + rate-limit backoff** | `jsa/db/repo.py`, `jsa/pipeline/orchestrator.py` | In `backend_switch_reset`, **retain** `Message` rows when the fallback backend is history-capable (anthropic) so `restore_session` replays instead of re-running the stage; only nuke history for no-replay targets. Add exponential backoff+retry on `RateLimitError` (and a shared provider `Semaphore`/token-bucket separate from the job-slot semaphore) **before** treating a limit as "switch backend". *(These two findings are merged — they rewrite the same limit-handling control flow.)* | P1/P2 |
| 7 | **API request hardening** | `jsa/api/routes_jobs.py` | Add `constr(max_length=…)` (or `Field(max_length=…)`) to `AnswerBody.text` / `ReviseBody.text` → 422 on oversized paste; drop `jd` from the `/api/jobs` list payload (`_job_to_dict(full=False)`) — it's already on `/api/jobs/{id}`. | P1/P2 |
| 8 | **Concurrency + scheduling knobs** | `jsa/config.py`, `jsa/server.py`, `jsa/db/repo.py` | Add a `max_parallel` `Settings` field (`JSA_MAX_PARALLEL`, default 5) and pass it into the `Orchestrator(...)` construction in `server.py`. Change `list_runnable_jobs` ORDER BY to `tier ASC, updated_at ASC` so tier-A employers run first. | P1 |
| 9 | **Event-bus bounding + shutdown drain** | `jsa/events/bus.py`, `jsa/server.py` | Give subscriber queues a `maxsize` with drop-oldest (or disconnect-on-overflow) so a slow-but-connected WS client can't grow memory unbounded. In `_shutdown`, track and `await asyncio.gather(...)` in-flight `_run_one` tasks (bounded) before `engine.dispose()` so in-flight commits don't hit a disposed engine. | P2/P3 |
| 10 | **Fit-gate optional + CV-prefix cache** | `jsa/config.py` (or `jsa/store/preferences.py`), `jsa/pipeline/orchestrator.py`, `jsa/pipeline/stages.py` | Add a backend-only `enable_fit_assessment` flag (mirror the preferences/Settings pattern); when off, `_next_stage_for` routes `pending→cv_adjust` directly. Prompt-cache the shared CV markdown prefix across all fit-assessments (one write, N cheap hits). **Backend-only — no UI toggle** (a UI surface would pull in the `npm run build` bundle step and break independence). | P2 |
| 11 | **Per-stage wall-clock budget** | `jsa/pipeline/stages.py` | Wrap each stage's agent work in an overall `asyncio.wait_for(...)` budget (or cap total agent calls per stage) so a pathological job chaining research+main+self-heals can't hold a slot for ~35 min and starve throughput. | P2 |

### Shared-file notes (for worker prompts)

Merging is deferred, so overlap is fine — but each worker must make **minimal, localized** edits:
- `jsa/pipeline/orchestrator.py`: U2 (run loop), U6 (`_handle_limit_reached`), U10 (`_next_stage_for`) — different functions.
- `jsa/server.py`: U2 (task supervision + startup `recovery_sweep`) & U8 (pass `max_parallel`) both touch `_startup` near the `Orchestrator(...)` construction (lines 113–125) → **edit surgically**; U9 touches `_shutdown`.
- `jsa/db/repo.py`: U6 (`backend_switch_reset`) vs U8 (`list_runnable_jobs`) — different functions.
- `jsa/config.py`: U4 (`max_tokens`), U8 (`max_parallel`), U10 (`enable_fit_assessment`) — adjacent field additions.
- `jsa/pipeline/stages.py`: U10 (fit) vs U11 (stage budget) — different regions.

## E2e verification recipe

**Common setup for every unit** (run first, in the worktree):
1. `pip install -e .`
2. `pytest -v -m "not integration"` — **≈27 backend tests already fail on `main`** (pre-existing
   fit-gate + JSON-fixture debt, unrelated to this work). Only ensure your change adds **no new
   failures**; do **not** fix pre-existing debt.
3. Standard CLI smoke (baseline liveness): start the daemon with the project's standard test
   command (CV + CSV paths, `--no-browser`), then `curl http://localhost:8765/api/health` →
   `{"ok":true}` and `curl http://localhost:8765/api/jobs`.

**Per-unit real e2e (do these — cheap and concrete):**
- **U1 (WAL):** after any run, open the sqlite DB and run `PRAGMA journal_mode;` → expect `wal`;
  `PRAGMA busy_timeout;` → `5000`.
- **U3 (CSV):** craft a CSV whose JD field is >128 KB (and one malformed row); start the daemon →
  it must boot, that row soft-skipped into the `errors`/warnings channel, other jobs ingested.
- **U7 (API):** `curl -XPOST /api/jobs/{id}/answer` (and `/revise`) with a multi-MB `text` → expect
  422/413, not a 500/failed job. `curl /api/jobs` → assert **no `jd` key** in list items;
  `curl /api/jobs/{id}` → `jd` still present.
- **U8 (knobs):** set `JSA_MAX_PARALLEL=2`, start daemon, confirm the orchestrator semaphore value /
  logged concurrency reflects 2; verify `list_runnable_jobs` returns tier-A before tier-B/C.
- **U5 (CLI stdin):** unit-drive `start_session`/`send_message` with a >1 MB prompt and assert no
  `OSError: Argument list too long`; assert the subprocess received the payload on stdin.

**Unit-test + code-review only (skip live e2e — no cheap path without live API keys / heavy fixtures):**
- **U2** (dispatcher liveness): unit-test the loop guard (inject a `_next_stage_for` `ValueError`,
  assert the loop survives + slot released + other jobs still dispatch) and the startup
  `recovery_sweep`. Skip live e2e.
- **U4** (prompt caching / max_tokens / exception mapping): unit-test the content-block/cache_control
  structure, `max_tokens` wiring, `stop_reason` handling, and content-guard with a fake response.
  Skip live e2e (needs real Anthropic billing to observe cache hits).
- **U6** (backend-switch history + backoff): unit-test with `FakeAgentBackend` that history is
  retained for a history-capable fallback and backoff fires before switching. Skip live e2e.
- **U9** (event-bus bound + drain): unit-test queue overflow behavior and shutdown drain. Skip live e2e.
- **U10** (fit gate off + cache): unit-test `_next_stage_for` routes `pending→cv_adjust` when the flag
  is off. Skip live e2e.
- **U11** (stage budget): unit-test that a stage exceeding the budget raises/aborts cleanly. Skip live e2e.

## MANDATORY conventions every worker must honor (from CLAUDE.md)

- **Never** set `Job.state` / `Job.current_stage` directly — always `transition(job, new_state, new_stage)`
  (`jsa/pipeline/state_machine.py`). (U2, U6, U10)
- Every state-changing write is **one atomic** `repo.checkpoint(...)` — never separate commits. (U2, U6)
- Any sync/blocking/CPU-bound call wrapped in `await asyncio.to_thread(...)`. (U3 CSV parsing, U5 subprocess helpers)
- Use **fakes over mocks** in tests (`tests/backend/fakes/fake_backend.py::FakeAgentBackend`); follow TDD.
- Backend-only changes — do **not** add frontend UI (would trigger the `npm run build` bundle step
  and break unit independence).

## Worker instruction template (given to each agent, verbatim tail)

Each agent gets: the overall goal, its unit row (title/files/change) copied verbatim, the shared-file
note for any file it touches, the mandatory-conventions block, its e2e recipe, and this tail:

```
After you finish implementing the change:
1. Run unit tests — pip install -e . then pytest -v -m "not integration". ~27 backend tests
   already fail on main (pre-existing debt) — ensure your change adds NO NEW failures; do NOT
   fix pre-existing debt or drift out of scope.
2. Test end-to-end — Follow the e2e recipe above. If it says "skip live e2e", unit tests are
   sufficient; say so.
3. Commit LOCALLY — commit all changes to your worktree branch with a clear message. DO NOT push.
   DO NOT create a PR or MR (the user explicitly forbade this). End commit messages with the
   project's Co-Authored-By trailer.
4. Report — End with a single line: BRANCH: <branch-name> @ <short-sha>. If you could not commit,
   end with BRANCH: none — <reason>.
```

Workers do **not** run the `code-review` skill themselves (would burn tokens 11x). Code review is
centralized — the supervisor (coordinator session) runs it once per worktree after all workers finish.

## Post-batch

1. After all 11 report, render a status table (Unit / Status / Branch@sha) and a one-line summary.
2. **Supervisor-run code review**: for each of the 11 worktrees, the coordinator invokes the
   `code-review` skill (high effort, `--fix`) against that worktree's diff, one at a time (or a small
   batch), and applies fixes directly in that worktree. Re-run the unit's test command afterward to
   confirm the fix didn't regress anything.
3. Render the final table including a Review column (clean / fixed N / n/a) and a one-line summary.
   Branches remain local in their worktrees for the user to review and merge manually — no PRs opened.
