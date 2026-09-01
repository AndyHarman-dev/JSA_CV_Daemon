# Babysit ledger — .claude/plans/radiant-strolling-curry.md

Started: 2026-09-01 20:34
Plan: Model-level fallback ladder + rate-limit containment
Base branch: `feat/multi-backend-model-select` → target branch `feat/model-fallback-ladder`

## Environment note (found before any work)

`python3` / `pytest` on PATH is homebrew 3.14 **without `pytest-asyncio`** — running the
suite there produces 518 failed / 335 errors that are pure environment noise
(`PytestConfigWarning: Unknown config option: asyncio_mode`). **The correct interpreter is
`.venv/bin/pytest` (Python 3.11.15)**, where `jsa` resolves to the repo checkout. Every
check in this ledger uses `.venv/bin/pytest`.

## Baseline (before any change)

| Suite | Command | Result |
|---|---|---|
| Backend | `.venv/bin/pytest -q -m "not integration"` | 1552 passed, **1 flaky failed**, 2 skipped, 21 deselected (both runs) |
| Frontend | `cd frontend && npm test` | **280 passed (21 files)**, 0 failed |

**The one backend failure is nondeterministic noise, not debt — and it is a DIFFERENT test
each run.** Run 1 failed `test_integration.py::TestParkAndResume::test_job_parks_at_awaiting_input`;
run 2 failed `test_dev_tunnel.py::TestStartTunnelUrlPrinted::test_daemon_thread_is_started`.
Both are timing/thread-sensitive. The first passes in isolation (1 passed) and 3/3 on
repeated whole-file runs (16 passed each).

**Consequence for grading every later phase:** "1552 passed / 1 failed" is a *green*
baseline. A phase is only a regression if a check fails **reproducibly** — a single
full-suite failure must be re-run in isolation before it is called a regression, and the
failing test's identity compared against this list.

## Phases

| # | Phase | Model | Check | Review | Status | Attempts |
|---|-------|-------|-------|--------|--------|----------|
| 1 | Rate-limit containment (per-backend cap, stagger, 180→300s) | unspecified → self | `.venv/bin/pytest tests/backend/test_orchestrator_throttling.py -v` | high (planned) | pending | 0 |
| 2 | Cost-ordered model ladder data | unspecified → self | `.venv/bin/pytest tests/backend/test_model_costs.py -v` | medium (planned) | pending | 0 |
| 3 | Per-job model tracking (`model_name`, `model_hops`) | unspecified → self | `.venv/bin/pytest` targeted (columns, factory, `_wrap_factory`, resets) | high (planned) | pending | 0 |
| 4 | Model-first fallback in the BF-19 funnel | unspecified → self | `.venv/bin/pytest tests/backend/test_bf18_limit_detection.py -v` | high (planned) | pending | 0 |
| 5 | Observability and docs | unspecified → self | `cd frontend && npm test && npm run build` | medium (planned) | pending | 0 |

No phase names a model → routing is the babysitter's call. Running phases in-session
rather than via cold subagents: this repo's CLAUDE.md carries many explicit
"do not simplify this back" invariants (zen's three-way classification, OpenRouter's
`require_parameters`, instance-level `supports_structured_output`, `_run_fit_assessment`'s
narrow `except`) that a fresh subagent would likely trip.

## Pre-dispatch probes (plan claims verified independently)

| Claim | Source in plan | Verified |
|---|---|---|
| `_PROTOCOL` has 23 models = 15 chat + 8 messages | Phase 2 / Design decisions | ✅ counted in `jsa/agents/opencode_go.py` |
| `ALLOWED[running]` covers `pending`/`cv_done`/`review` (makes a same-backend `backend_switch_reset` legal) | Design decisions | ✅ `state_machine.py:21` |
| `backend_switch_reset` commits internally (so a post-call assignment is a 2nd txn) | Phase 4 [skeptic] | ✅ `repo.py` ends with `await session.commit()` |
| `_wrap_factory` counts only params with `default is empty` (so `(name, model=None)` counts 1) | Phase 3 [skeptic] | ✅ `orchestrator.py:66-75` |
| `_fetch_openrouter` downloads pricing and discards it | Phase 2 | ✅ `model_catalog.py:159-163` — keeps only `id` |
| Three post-acquire bailouts each do `self.sem.release(); continue` | Phase 1 | ✅ `db_job is None`, `state == running`, transition `except` |
| `self.wakeup.clear()` sits above the dispatch scan (starvation-freedom) | Phase 1 [skeptic] | ✅ `orchestrator.py:164` |
| OpenCode publishes per-model output pricing (Phase 2 feasibility) | Phase 2 | ✅ opencode.ai/docs/en/go/ lists per-1M input/output for 26 models |

**Phase 2 residual gap:** the docs page prices 26 models but `minimax-m2.5` (in `_PROTOCOL`,
`messages` protocol) is **not** among them. 22/23 rungs have published prices; that one
needs a documented stand-in. Not a blocker — surfaced here so it isn't silently invented.

## Diagnostic (plan Verification item 4, run early)

`sqlite3 ~/.jsa/jsa.sqlite "select company, state, substr(error,1,90) from jobs where state='failed'"`

```
Innowise|failed|Backend timed out on every configured backend — switch backends or increase the timeout
```

**This validates the plan's diagnosis.** The surviving failure is a *timeout*, not
"limit reached" — so it takes the ladder path (Phase 4 excludes only `AgentLimitReached`).
Only one of the three original failures is still in the DB in `failed` state; the others
were presumably reset or re-run.

## Log

20:34 Read plan in full. Verifiability gate assessed → **passes**: every phase names a
      runnable check (new test files with enumerated assertions, `npm test`, `npm run build`).
      The only non-runnable deliverables — the CLAUDE.md BF-19 subsection and the Phase 2
      module docstring — enumerate their required content concretely enough to grade by reading.
20:35 First baseline attempt useless: `timeout` not on macOS, then `python` not on PATH,
      then homebrew `pytest` missing `pytest-asyncio`. Found `.venv/bin/pytest` — real baseline.
20:40 Ran 8 independent verifications of plan claims (table above). All matched. No plan
      drift found before starting.
20:42 Fetched OpenCode Go pricing docs → Phase 2 is feasible, not blocked.
20:43 **HALTED before Phase 1** — dirty working tree (~50 uncommitted files from the prior
      multi-backend-model-select plan). See halt entry below.
