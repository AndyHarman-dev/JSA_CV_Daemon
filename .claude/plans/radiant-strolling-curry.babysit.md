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
| 1 | Rate-limit containment (per-backend cap, stagger, 180→300s) | self (Opus 5) | `.venv/bin/pytest -m "not integration"` → **1570 passed, 0 failed** | medium (running) | check green, review pending | 1 |
| 2 | Cost-ordered model ladder data | Sonnet subagent | `.venv/bin/pytest tests/backend/test_model_costs.py -v` | ↓ batched | dispatched | 1 |
| 3 | Per-job model tracking (`model_name`, `model_hops`) | Sonnet subagent | `.venv/bin/pytest` targeted (columns, factory, `_wrap_factory`, resets) | **medium, covers 2+3** | pending | 0 |
| 4 | Model-first fallback in the BF-19 funnel | Sonnet subagent | `.venv/bin/pytest tests/backend/test_bf18_limit_detection.py -v` | ↓ batched | pending | 0 |
| 5 | Observability and docs | Sonnet subagent | `cd frontend && npm test && npm run build` | **medium, covers 4+5** | pending | 0 |

**Routing.** No phase names a model, so routing was the babysitter's call. Phase 1 was run
in-session; **at the user's instruction (mid-run), Phases 2–5 dispatch to Sonnet 5
subagents**, with verification, review-draining and re-verification staying in the
babysitter's hands. Each subagent brief must carry the repo's relevant "do not simplify
this back" invariants explicitly (zen's three-way classification, OpenRouter's
`require_parameters`, instance-level `supports_structured_output`,
`_run_fit_assessment`'s narrow `except`), since a cold subagent has not read CLAUDE.md.

**Review policy (user instructions, mid-run).** Two overrides of the babysit skill's
defaults, both deliberate:
1. `/code-review high` is too token-expensive → reviews run at **low or medium only**.
2. Reviews are **batched across phases, not run per phase** → one `medium` review covering
   Phases 2+3, one `medium` covering Phases 4+5. Phase 1's review was already in flight when
   this landed and was left running as its own.

**What batching costs, stated plainly:** the babysit skill's "drain the review before the
next phase" rule exists because `--fix` edits code a later phase then builds on. With
batching, Phase 3 is written against un-reviewed Phase 2 code. The mitigation is that the
batched review's fixes get **re-verified against BOTH phases' checks** before moving on, so
a fix that breaks Phase 2 is still caught — just later, and with a wider blast radius to
untangle if it happens.

All implementation subagents run on **Sonnet**. (The `/code-review` reviewer's model is
chosen by that skill, not settable from the invocation.)

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
      multi-backend-model-select plan). User chose "commit prior work first". Scanned the
      tree for secrets before committing (clean; `cv_structure_example.json` is a fictional
      example CV, not the user's). Committed as `0c2092e`, branched `feat/model-fallback-ladder`.
20:52 Phase 1 implemented in-session. Deviation from the plan's wording, deliberate: the
      per-backend cap is a **plain int counter dict** (`_backend_inflight`), not an
      `asyncio.Semaphore`. The plan's actual requirement is a *non-blocking* acquire, and
      `asyncio.Semaphore` has no public non-blocking acquire — the alternatives were poking
      `sem._value` (private) or a `locked()`-then-`await acquire()` dance that only works by
      relying on implementation detail. A counter expresses "try, else skip" directly and is
      trivially assertable in tests. Same mechanism, same four release sites.
20:53 Dropped a `kick()` I had first added to the transition-failure bailout after checking
      the reasoning: reaching any bailout means the acquire SUCCEEDED, i.e. the backend had
      room, which in a sequential scan means no earlier job was skipped on its account. So
      the release cannot unblock anything already walked past. Documented in-code so it
      isn't "fixed" back in.
20:55 Phase 1 check: `test_orchestrator_throttling.py` → **17 passed**.
      **Mutation-tested the check itself** (made `_acquire_backend_slot` always return True):
      5 of the 17 fail. The tests have teeth; they are not asserting tautologies.
20:57 Full suite → 1 real regression: `test_opencode_zen.py::test_opencode_zen_timeout_default`
      pinned `opencode_zen_timeout == 180.0`. **Updated to 300.0** — this is the plan's own
      locked decision, i.e. the test encoded the OLD contract, not a criterion I softened.
      Checked the neighbours: `test_mistral.py::test_default_timeout` pins
      `MistralBackend()._timeout == 180.0`, which is the **constructor** default, a different
      thing the plan does not touch — left alone, and it still passes.
      *(Observation, not acted on: the backend classes' own constructor defaults are still
      180s while Settings is now 300s. Harmless — server.py always passes the settings value
      — but worth a look in Phase 5's doc pass.)*
20:58 **Phase 1 check re-run after the test fix: 1570 passed, 0 failed, 2 skipped.**
      Committed as `440f6fa`.
20:59 Scraped OpenCode Go's raw pricing HTML → `scratchpad/opencode_go_pricing.md`.
      **All 23 `_PROTOCOL` models have published prices** — the Phase 2 gap flagged earlier
      is closed. Important: the gap was an artifact of LLM *summarisation*, not the source —
      one WebFetch omitted `minimax-m2.5` and a second guessed a price for it. The raw table
      lists it at $0.30/$1.20. Phase 2's subagent gets the scraped table, not a fetch task.
21:02 Launched `/code-review high --fix` for Phase 1 — **user stopped it**: high is too
      token-expensive. New standing policy recorded above (low/medium only, big phases only).
      Relaunching Phase 1's review at `medium`.
21:30 Phase 2 implementation found already staged in the working tree (`jsa/agents/model_costs.py`,
      modified `jsa/agents/model_catalog.py`, `tests/backend/test_model_costs.py`). Verified
      against the plan: all 23 `_PROTOCOL` models priced, stable/unknown-last ordering,
      OpenRouter live-pricing opportunistically wired.
21:35 `.venv/bin/pytest tests/backend/test_model_costs.py -v` → 17 passed.
      `.venv/bin/pytest tests/backend/test_model_catalog.py -v` → 26 passed.
      `.venv/bin/pytest -q -m "not integration"` → 1587 passed, 0 failed, 2 skipped (+17 over Phase 1).
21:40 Advisor review: GO — commit as-is. Committed Phase 2 as `32a84c1`. Updated plan Change Log.
      Phase 1 test-file tweak (`tests/backend/test_orchestrator_throttling.py`) remains unstaged;
      it is unrelated to Phase 2.
