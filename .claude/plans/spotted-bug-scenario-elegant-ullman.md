---
status: Done
---

# Fix: dismissed jobs resurrect themselves with a stale NEED_INPUT

## Context

**Bug:** Run the app with a CV + CSV so 5 jobs start in parallel. Before they emit
anything, dismiss 4 of them. They show as `dismissed` — but after a short wait the
dismissed jobs pop a NEED_INPUT request as if never dismissed.

**Root cause (cross-confirmed by three explorations + code reads):** a data race
between the dismiss request and the in-flight worker.

1. The orchestrator worker `_run_one` (`jsa/pipeline/orchestrator.py:208`) loads a `Job`
   ORM object once (`L215`) into its session and holds that same in-memory object across
   the entire — slow — agent call. Its `.state` stays `running`.
2. `POST /api/jobs/{id}/dismiss` (`jsa/api/routes_jobs.py:303`) runs in a *separate*
   session and commits `running → dismissed`. It does **not** cancel the worker task
   (the orchestrator's `_tasks` is an unkeyed `set` — `orchestrator.py:114,202`, no
   `job_id → Task` map, no `.cancel()` anywhere).
3. When the agent finally returns NEED_INPUT, `_handle_needs_input`
   (`jsa/pipeline/stages.py:550`) calls `checkpoint(... awaiting_input ...)`
   (`stages.py:575`). `repo.checkpoint` (`jsa/db/repo.py:358`) validates only via
   `transition(job, awaiting_input)` (`repo.py:394`) against the **stale in-memory
   `running`** — which is a legal transition (`state_machine.py:15`) — then commits,
   **overwriting the `dismissed` row** and inserting a fresh FollowUp. The NEED_INPUT
   reappears.

There is no optimistic-lock column, no fresh re-read of DB state before the completion
checkpoint, and `expire_on_commit=False` (`jsa/db/engine.py:19,68`) means the ORM object
is never refreshed. The `cancel` route docstring (`routes_jobs.py:389-395`) already
documents this exact class of bug as "out of scope for this phase."

**Intended outcome:** a dismissed (or cancelled) job stays dismissed — a stale worker
result is discarded, never written. Additionally, dismiss cancels the in-flight agent
task so it stops consuming API tokens immediately.

## Fix

Two parts: a **correctness guard** (backstop that fully fixes the symptom) and
**task cancellation** (cost optimization the user requested).

### Part A — Fresh-session state guard (correctness backstop)

Before the worker writes its completion checkpoint, re-read the job's authoritative state
**from a fresh session** and discard the result if the job is no longer `running`.

> Why a fresh session, not the worker's own: the worker session opened a read transaction
> at `get_job` (`orchestrator.py:215`) that stays open across the agent call, so a
> same-session `SELECT` can return the stale snapshot (`running`) and silently no-op the
> guard. A fresh short-lived session sees the committed `dismissed`. (Same pattern the
> dispatch loop already uses at `orchestrator.py:147-161`.)

1. **`jsa/db/repo.py`** — add a helper that reads state on a fresh session derived from the
   caller's bind (no factory threading):
   ```python
   async def get_state_fresh(session: AsyncSession, job_id: str) -> JobState | None:
       """Read Job.state from a fresh session so a concurrently-committed change
       (e.g. dismiss) is visible even if the caller's transaction holds a stale snapshot."""
       async with AsyncSession(session.get_bind(), expire_on_commit=False) as fresh:
           return (
               await fresh.execute(select(Job.state).where(Job.id == job_id))
           ).scalar_one_or_none()
   ```

2. **`jsa/pipeline/stages.py`** — define a benign signal exception next to `PausedForInput`
   (`stages.py:50-53`):
   ```python
   class StaleJobResult(Exception):
       """Raised when the job's DB state changed (dismiss/cancel/delete) while the agent
       was working — the worker's result is stale and must be discarded, not written."""
   ```

3. **`jsa/pipeline/stages.py` → `run_stage`** — right **after** the agent `reply` is
   obtained and **before** the `if reply.kind == "needs_input"` branch (~`stages.py:457`),
   add the guard. Placement here (not inside `checkpoint`) covers **both** the NEED_INPUT
   and FINAL paths, and avoids the post-write `fu_result.scalar_one()` at `stages.py:478`
   blowing up with `NoResultFound` (which would mark the job *failed*):
   ```python
   current_state = await repo.get_state_fresh(session, job.id)
   if current_state != JobState.running:
       raise StaleJobResult(job.id, current_state)
   ```

4. **`jsa/pipeline/orchestrator.py` → `_run_one`** — catch `StaleJobResult` as a benign
   no-op alongside `PausedForInput` (`orchestrator.py:251-253`): log at info/debug and
   `pass`. The `finally` (`L283-285`) still releases the semaphore and kicks the loop.

This guard alone fully fixes the reported symptom and, as a bonus, closes the identical
`cancel` race documented at `routes_jobs.py:389-395` (guard triggers on `state != running`,
not just `dismissed`).

### Part B — Cancel the in-flight task on dismiss (stop burning tokens)

5. **`jsa/pipeline/orchestrator.py`** — change the task registry from an unkeyed set to a
   `job_id → Task` map:
   - `self._tasks: dict[str, asyncio.Task] = {}` (`L114`).
   - At spawn (`L202-204`): `self._tasks[job.id] = task`, and change the done-callback to
     remove by key: `task.add_done_callback(lambda t, jid=job.id: self._tasks.pop(jid, None))`.
   - Add a method:
     ```python
     def cancel_task(self, job_id: str) -> bool:
         t = self._tasks.get(job_id)
         if t is not None and not t.done():
             t.cancel()
             return True
         return False
     ```
   - In `_run_one`, do **not** swallow `asyncio.CancelledError` as a failure. It is a
     `BaseException`, so the existing `except Exception` (`L263`) won't catch it — verify it
     propagates cleanly and the `finally` still runs. If any explicit handling is added, log
     and re-raise. Do **not** route it through `mark_failed`.

6. **`jsa/api/routes_jobs.py` → `dismiss_job`** (`L303-337`) — after committing `dismissed`
   (`L318`), cancel the worker task, mirroring the existing accessor used by peer routes
   (`request.app.state.orchestrator`, e.g. `routes_jobs.py:428`):
   ```python
   request.app.state.orchestrator.cancel_task(job_id)
   ```
   Order matters: commit `dismissed` **first** (DB is authoritative), then cancel. If the
   task races to completion in the gap, Part A's guard still prevents resurrection — the two
   parts are complementary, cancellation being best-effort.

> Note on subprocess teardown: cancelling the asyncio task interrupts the `await` on the
> agent call; the underlying CLI subprocess may linger briefly and is cleaned up
> best-effort by the backend. Acceptable for a single-user local tool; the guard is the
> correctness guarantee regardless.

## Files touched

- `jsa/db/repo.py` — new `get_state_fresh` helper.
- `jsa/pipeline/stages.py` — `StaleJobResult` exception + guard in `run_stage`.
- `jsa/pipeline/orchestrator.py` — keyed `_tasks` dict, `cancel_task`, benign
  `StaleJobResult` handling, clean `CancelledError` propagation.
- `jsa/api/routes_jobs.py` — `dismiss_job` cancels the task after commit.
- Tests (below).

## Tests (TDD — write alongside)

New file `tests/backend/test_dismiss_race.py`, using `FakeAgentBackend`
(`tests/backend/fakes/fake_backend.py`) per project convention:

1. **Guard discards stale NEED_INPUT (the reported bug).** Must faithfully mirror the real
   session lifecycle or it won't reproduce the race:
   - Session A: `get_job` for a `running` job (like `orchestrator.py:215`).
   - Session **B** (separate): `checkpoint(... dismissed ...)`.
   - Session A: drive `run_stage` with a fake backend returning `needs_input`.
   - Assert: job stays `dismissed`; no `awaiting_input`; **no** open FollowUp row created;
     `StaleJobResult` raised.
   - Sanity check: this test must *fail* if the guard reads state on the worker's own
     session instead of a fresh one — otherwise it isn't faithful.
2. **Guard also protects FINAL** (dismiss during `cv_adjust`): fake backend returns `final`;
   assert the job stays `dismissed`, no `cv_done`, no Document written.
3. **`cancel_task`**: spawn a worker (or stub a long `asyncio.sleep` task in `_tasks`),
   call `cancel_task(job_id)`, assert it returns `True`, the task ends, `_run_one` does
   **not** mark the job failed, and the semaphore is released.
4. Regression: existing `tests/backend/test_orchestrator.py`,
   `test_cancel_bf6.py`, and `TestDismissTransitions` in `test_bugfixes_bf1.py` still pass.

## Verification

1. `pip install -e .` then `pytest tests/backend/test_dismiss_race.py -v` — new tests green.
2. `pytest -v -m "not integration"` — full backend suite green (note: ~45 backend tests are
   pre-existing failures per memory `preexisting-test-debt`; compare against the baseline, do
   not attribute those to this change).
3. Manual E2E (the exact repro):
   `jsa --csv <jobs.csv> --cv <resume.pdf>` → 5 jobs start → dismiss 4 immediately →
   wait past when they'd normally hit NEED_INPUT → confirm the 4 stay `dismissed`, no modal
   / follow-up reappears, and only the 1 remaining job proceeds. With Part B, the dismissed
   jobs' agent activity should stop promptly rather than running to completion.
