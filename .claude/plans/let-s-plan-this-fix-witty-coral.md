---
status: InProgress
---

# Make cancellation actually kill CLI subprocesses (stop the token burn)

## Context

`Orchestrator.cancel_task()` (`jsa/pipeline/orchestrator.py:122`) calls `task.cancel()`, but that
cancellation cannot stop the API-token burn. Both CLI backends run their subprocess via a **blocking
`subprocess.run()` inside `asyncio.to_thread(...)`**. `task.cancel()` only injects `CancelledError` at
the next `await` boundary in the event loop — the worker thread sitting in `subprocess.run` is not one, so
the underlying `claude` / `agy` process runs to completion and keeps consuming quota. The orchestrator
already handles `CancelledError` correctly (`_run_one`, line 287) and `StaleJobResult` already protects
*correctness*; the only gap is **cost** — the child process is never killed.

**Goal:** convert both CLI backends to a real async subprocess with a killable process handle, so that on
`task.cancel()` (and on timeout) the child process is actually terminated and token burn stops promptly.

### Critical constraint discovered — must kill the process *group*, not just the child

`~/.local/bin/claude` is a **shim** (Mach-O launcher) that spawns a versioned child which does the real API
call (verified via `ps`: `…/bin/claude → …/versions/2.1.198 → …/versions/2.1.198`). Killing only the
top-level process (`proc.kill()`) would orphan the real token-burner. Therefore the spawn **must** use
`start_new_session=True` and kill the whole process group with `os.killpg(os.getpgid(proc.pid), SIGKILL)`.
`agy` is treated the same way (process-group kill is strictly safer regardless of its internal structure).

### Decisions (chosen while user was away — revisit if desired)
- **Cancel route scope:** *also* wire the user-facing `POST /api/jobs/{id}/cancel` route to call
  `cancel_task()`. Today only the **dismiss** route does; the cancel route just sets `running → pending`
  and resumes from checkpoint. Adding `cancel_task()` there makes the Cancel button stop token burn
  immediately, then resume cleanly from the last checkpoint on the next `kick()`.
- **Structure:** extract one shared async spawn helper so killability lives (and is tested) in a single
  place, minimizing test churn.

---

## Approach

### 1. New shared spawn seam — `jsa/agents/_subprocess.py`

A single async helper that both backends call. It owns spawning, timeout, and killability; each backend
keeps its own result-parsing (Claude returns `str`, Google returns a `dict` — different shapes, kept local).

```python
import asyncio, os, signal
from jsa.agents.base import AgentTimeout

async def run_killable(
    cmd: list[str], *, timeout: float, cwd: str | None = None, label: str = "subprocess",
) -> tuple[int, bytes, bytes]:
    """Spawn cmd as an async subprocess in its own process group and return
    (returncode, stdout, stderr). Kills the whole process group on timeout OR
    on external cancellation (CancelledError) so no orphaned child keeps burning
    API quota. Raises AgentTimeout on timeout; re-raises CancelledError on cancel."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,          # own process group → killpg works
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        raise AgentTimeout(f"{label} timed out after {timeout}s") from None
    finally:
        if proc.returncode is None:      # runs on timeout AND on CancelledError
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass                     # already exited between check and kill
            await proc.wait()
    return proc.returncode, out, err
```

Notes:
- The `finally` unifies both exit paths: timeout raises `AgentTimeout`; an external `task.cancel()`
  propagates `CancelledError` out of `wait_for`, the `finally` kills the group, then `CancelledError`
  continues to propagate (re-raised implicitly) — exactly what `_run_one` expects.
- `communicate()` drains both pipes concurrently → no PIPE-fill deadlock.
- Model to imitate already in-repo: `AnthropicAPIBackend._call_api` (`anthropic_api.py:94`) uses the same
  `asyncio.wait_for(..., timeout) / finally-cleanup` shape.

### 2. `jsa/agents/claude_cli.py`

- Make `_run` **async**; replace the `subprocess.run(...)` block with:
  ```python
  rc, out, err = await run_killable(cmd, timeout=eff_timeout, cwd=str(cwd) if cwd else None, label="claude CLI")
  ```
  Keep the existing decode + error mapping (`ClaudeSessionExpiredError` / `ClaudeCliError`, stderr logging)
  verbatim, sourcing from `rc / out / err` instead of `result.returncode / result.stdout / result.stderr`.
- Drop the `subprocess` import if now unused; import `run_killable`.
- Update **every** call site from `await asyncio.to_thread(self._run, ...)` → `await self._run(...)`:
  `_parse_with_nudge` (line 155), `start_session` (182), `send_message` (235), `run_research` (257).

### 3. `jsa/agents/google_cli.py`

- Same conversion. `_run` keeps its `dict` return shape and `log_path` handling; only the spawn changes:
  ```python
  rc, out, err = await run_killable(cmd, timeout=eff_timeout, label="agy CLI")
  ```
  (Google `_run` has no `cwd`.) `_extract_conversation_id(log_path)` (small sync file read) stays as-is.
- Update call sites → `await self._run(...)`: nudge (line 181), `start_session` (210), `send_message`
  (269), `run_research` (300).

### 4. `jsa/api/routes_jobs.py` — wire the Cancel button (chosen scope)

In `cancel_job` (`POST /api/jobs/{id}/cancel`, line 392): after the `running → pending` checkpoint and
before/with `kick()`, add `request.app.state.orchestrator.cancel_task(job_id)` so the in-flight process is
actually killed. Update the now-stale "out of scope for this phase" docstring (lines 396–402). The dismiss
route (line 342) already calls `cancel_task()` and needs no change — it just becomes effective.

---

## Tests

- **New:** `tests/backend/test_subprocess_killable.py` — the one place that verifies real killability:
  - Spawn a genuinely long child (e.g. `["python", "-c", "import time; time.sleep(30)"]`) via
    `run_killable`, wrap the awaiting task, `task.cancel()`, and assert it finishes quickly **and** the
    process group is gone (`os.killpg(pgid, 0)` → `ProcessLookupError`).
  - Timeout path: short `timeout` on the sleeper → `pytest.raises(AgentTimeout)`, process killed.
- **Rewrite** `tests/backend/test_cli_backends.py` (and the subprocess-touching parts of
  `test_google_cli_bf5.py`, `test_bf18_limit_detection.py`, `test_research.py`): they currently
  `patch("jsa.agents.claude_cli.subprocess.run", ...)` / `.google_cli.subprocess.run` and inspect a
  `CompletedProcess`-shaped `MagicMock`. After the refactor, patch the seam instead —
  `patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=(rc, out, err)))` — a plain
  `(int, bytes, bytes)` tuple, no full async-proc fake needed. Command-shape assertions move from
  `mock_run.call_args.args[0]` to the patched helper's `call_args.args[0]`. Timeout tests use
  `side_effect=AgentTimeout(...)`.
- **Extend** `tests/backend/test_dismiss_race.py::test_cancel_task_stops_worker_without_marking_failed`
  (line 244) is the closest existing coverage; keep it green.
- `FakeAgentBackend` (`tests/backend/fakes/fake_backend.py`) has no subprocess → unaffected.

---

## Verification (end-to-end — this is what proves the fix)

1. `pip install -e .` then `pytest -v -m "not integration"` — all suites green (esp. the rewritten
   `test_cli_backends.py` and new `test_subprocess_killable.py`).
2. **Real kill check** (the actual point of the change): start the server
   (`jsa --csv jobs.csv --cv resume.pdf --no-browser`), let a job reach an in-flight `claude`/`agy` turn,
   then hit Cancel (and separately, Dismiss). In another shell:
   ```
   ps -ef | grep -E 'claude|agy' | grep -v grep
   ```
   Confirm the spawned `claude`/`agy` process **and its versioned child** disappear within ~1s of the click
   — no lingering process → no continued token burn. Before the fix the process survives to completion.
3. Confirm the job lands in the expected state (dismiss → `dismissed`; cancel → `pending`, then resumes
   from checkpoint on the next dispatch) and is not marked `failed`.
4. `graphify update .` to refresh the graph after the code change.

---

## Out of scope
- `AnthropicAPIBackend` — already cancellable via `asyncio.wait_for` (`anthropic_api.py:105`); untouched.
- Any change to `StaleJobResult` / state-machine correctness backstops.
