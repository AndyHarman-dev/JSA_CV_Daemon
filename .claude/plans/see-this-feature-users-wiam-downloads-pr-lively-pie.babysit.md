# Babysit ledger — .claude/plans/see-this-feature-users-wiam-downloads-pr-lively-pie.md

Started: 2026-09-03
Worktree: `/Users/wiam/VSCodeProjects/JSA/.claude/worktrees/prompt-injection`
Branch: `feat/prompt-injection` (off `main` @ e9c278a)
Python: worktree-local `.venv` (see log — global editable install is broken)

## Baselines (recorded before any edit)

| Suite | Command | Result |
|---|---|---|
| Backend | `.venv/bin/pytest -q -m "not integration"` | 1779 passed, 2 skipped, 21 deselected |
| Frontend | `cd frontend && npm test` | 24 files, 342 passed |

## Phases

| # | Phase | Model | Check | Batch | Review | Status | Attempts |
|---|-------|-------|-------|-------|--------|--------|----------|
| 1 | Storage + API for `job.injection` | unspecified → Opus 5 sub | `pytest tests/backend/test_prompt_injection.py` → **44 passed** | B1 | — | check green, blocked — awaiting user | 1 |
| 2 | Prompt assembly + pipeline wiring (crux) | unspecified → Opus 5 sub | `pytest tests/backend/test_prompt_injection.py test_prompt_assembly.py test_prompt_prefix_stability.py` | B2 | — | pending | 0 |
| 3 | Preset ("dose") store + routes | unspecified → Opus 5 sub | `pytest tests/backend/test_injection_presets.py` → **20 passed** | B1 | — | check green, blocked — awaiting user | 1 |
| 4 | Frontend: syringe trigger + vial panel | unspecified → Opus 5 sub | `npm test` incl. `PromptInjector.test.tsx` + clamp unit test | B3 | — | pending | 0 |
| 5 | "JOB POSTING" link in review tab row | unspecified → Opus 5 sub | `npm test` incl. extended `ReviewPane.test.tsx` | B3 | — | pending | 0 |
| 6 | Documentation + verification | self | see "Phase 6 check" below | B3 | — | pending | 0 |

Review batches: **B1** = 1+3 `medium --fix` · **B2** = 2 alone `high --fix` · **B3** = 4+5+6 `medium --fix`

## Gate result

Phases 1–5 pass the verifiability gate as written (each names a runnable test file and
concrete assertions). **Phase 6's doc half had no check** — its "Verification" block is the
plan-wide check, not Phase 6's own. Proposed and adopted (shown to user, run started):

> **Phase 6 check** —
> 1. `.venv/bin/pytest tests/backend/test_prompt_prefix_stability.py tests/backend/test_prompt_assembly.py -v` passes, **and** `git diff` shows zero edits to existing cases in those two files (the plan's own stated proof that the invariant was narrowed, not broken);
> 2. `grep` confirms CLAUDE.md's cross-job-invariant paragraph no longer states the unqualified "no per-job bytes … ever", and carries the three-row reuse table;
> 3. `grep` confirms a `## Per-job prompt injection` section exists in CLAUDE.md.

## Log

20:40 Created worktree `.claude/worktrees/prompt-injection` on new branch `feat/prompt-injection` off `main` @ e9c278a.
20:41 Extracted `~/Downloads/prompt_injection_feature.zip` → scratchpad (`design_handoff_prompt_injection/README.md` + `JSA App Shell.dc.html`). Design reference for Phase 4.
20:42 **Environment breakage found, pre-existing and unrelated to this plan.** Global `pip` editable install of `jsa` points at `.claude/worktrees/agent-a26487e88e4a2e806`, a worktree that no longer exists → `import jsa` fails globally, so the user's global `jsa` CLI is currently broken. Additionally `pytest-asyncio` is not installed at all, so the raw global suite reported 593 failed / 378 errors — entirely an env artifact (`asyncio_mode` unknown-config warning + "requested an async fixture with no plugin"). Routed around with a worktree-local `.venv` (`python3 -m venv .venv`; `pip install -e . pytest pytest-asyncio`) rather than touching the global env — deliberately, per the known "worktree editable install hijacks global jsa" hazard. **Flagged to user; not treated as a halt.**
20:43 Baselines captured green in the venv: backend 1779 passed / 2 skipped; frontend 342 passed / 24 files.
20:44 Plan factual claims verified — all 22 referenced source/test files exist; `assemble_system_prompt` is at `prompt_assembly.py:207` with exactly the signature the plan quotes. Plan is accurate against the repo.
20:45 Playwright 1.62.1 available via `npx` with cached Chromium → smoke steps 2–5 are automatable at the end rather than user-action items.
20:45 Skill updated per user request: added "Bundle reviews across phases" to `~/.claude/skills/babysit/SKILL.md`.
20:55 **Phase 3 landed.** Verified personally: `.venv/bin/pytest -q tests/backend/test_injection_presets.py` → `20 passed`. Files: `jsa/store/injection_presets.py` (new), `jsa/api/routes_injection_presets.py` (new), `jsa/config.py` (+`injection_presets_path`), `jsa/server.py` (router registration), `tests/backend/test_injection_presets.py` (new, 20 tests).
      Agent resolved 3 gaps the plan left open, all defensible: caps live on the **route body** model not the store model (an over-cap file would otherwise make `load()` raise and permanently 500 the GET with no API repair path); response is the wrapped `{"presets": [...]}` matching both sibling routers rather than a bare array; caps extended to all six string fields, since an uncapped `name` is the same unbounded-file vector.
      **Open handoff to Phase 4:** preset `id` uniqueness is unspecified and unenforced — a client can PUT two presets sharing an `id`, and if Phase 4 uses it as a React key *and* the chip-delete handle, a collision deletes the wrong chip. Must be handled in Phase 4.
      **Flagged for verification once Phase 1 lands:** agent reports cross-file test-ordering pollution — `test_orchestrator.py::TestGoogleSessionExpiredAutoRecovery::test_auto_resets_once_then_fails_permanently` fails in the full suite but passes in isolation, and disappears when Phase 1's `test_prompt_injection.py` is excluded. Not accepted as "not mine" — to be reproduced and fixed before B1 review.
21:05 **Phase 1 landed** (44 tests, own file green) — but the B1 regression gate FAILED and both agents' "not mine / it disappeared" claims are refuted by measurement.
      - Phase 1 agent disclosed running `git stash -u` / `git stash pop` on the **shared** worktree while the Phase 3 agent was writing to it. Checked: stash stack holds 9 pre-existing entries from other branches/sessions, none orphaned by our run; Phase 3's tests re-verified green (20 passed) after the fact. No damage found, but this violated the shared-stash rule.
      - **Built a pristine baseline worktree** at e9c278a (`scratchpad/baseline`, own venv) to settle cause. Baseline full suite: **1779 passed, 0 failed, 3/3 runs — deterministically green.** The target test also passes 3/3 in isolation there.
      - Current tree, 3 consecutive full runs: `1 failed`, `1 failed`, **`2 failed`** (1842/1842/1841 passed). So our changes turned a deterministic suite **nondeterministically flaky** — that is a real regression, not pre-existing debt.
      - Failure: `TestGoogleSessionExpiredAutoRecovery::test_auto_resets_once_then_fails_permanently`, `assert refreshed.retry_count == 1` → got `0`. The auto-recovery *does* fire (log line present); the count is lost. Note `_run_fit_assessment` (`stages.py`) sets `job.retry_count = 0` in memory, so whether that value is flushed before the raise is order/timing sensitive — the likely mechanism.
      - Passes in every small pairing (target alone, whole `test_orchestrator.py`, paired with either new file) → whole-suite ordering/timing effect, not a direct interaction.
      **Not softening this test.** Isolating which new file perturbs it, then feeding the evidence back to the responsible agent.
21:20 **Root-caused the flakiness to Phase 3's test file, empirically — not to the plan's code and not to pre-existing debt.**
      - Attribution, 2/2 runs each way: ignoring `test_injection_presets.py` → `1823 passed, 0 failed` (clean); ignoring `test_prompt_injection.py` → `2 failed` / `1 failed`. File ordering explains why only Phase 3's file can reach it: `test_injection_presets` sorts *before* `test_orchestrator`, `test_prompt_injection` sorts after.
      - **Ruled out "cumulative load".** In the pristine baseline worktree I added two full duplicate copies of `test_api.py` — 104 extra tests using the identical `create_app` + `lifespan_context` fixture pattern, both sorting before `test_orchestrator.py`: **`1881 passed, 0 failed`, 3/3 runs.** So the copied fixture pattern is fine at 5× the test count; something specific in Phase 3's file leaks process-global state.
      - Second affected test surfaced: `test_orchestrator_throttling.py::TestSlotReleasePaths::test_vanished_job_bailout_releases_the_slot` (intermittent).
      - Sent back to the Phase 3 agent with the full reproduction (fix attempt 1 of 2). Explicit constraints: may not touch either orchestrator test file, may not relax/skip/xfail those assertions, may not use `git stash`, must keep all 20 of its own tests; must produce **three consecutive** green full runs. If it concludes the orchestrator test itself is wrong, it must stop and report rather than edit it — that is a user decision (Step 6), not its call.
      - Experiment scaffolding (`test_api_dup*.py` in the baseline worktree) removed; it was created by me in this task.
21:55 **RETRACTION + true root cause. My 21:20 conclusion was wrong; the Phase 3 agent's rebuttal was right.**
      The Phase 3 agent reopened the investigation and refuted the "Phase 3's file leaks process-global state" finding. It was correct, and it also correctly identified the statistical flaw in my 104-copy control: at the observed ~50% per-run failure rate, 3 green runs is p≈0.125 under the null — weak evidence, not a clean negative. I re-ran the experiment properly rather than accepting either the agent's report or my own earlier claim.
      **Decisive A/B, pristine baseline worktree @ e9c278a (zero plan code):**
      - **A — no changes at all, 10 full runs: 10/10 green** (1779 passed).
      - **B — plus a file of 20 content-free `assert True` no-ops** sorting before `test_orchestrator.py`, 14 runs total across two batches: **2 failures.** `1 failed, 1798 passed` (batch 1, identity not captured) and, in batch 2, `FAILED tests/backend/test_integration.py::TestHappyPath::test_both_jobs_approved_with_pdfs`.
      No app, no store, no `Settings`, no monkeypatch, no fixture — just 20 `assert True`. **The plan's code is fully exonerated.** The only causal variable is how many tests run before the orchestrator-driven tests; our two new files merely perturbed timing.
      **Confirmed the mechanism myself in source (not taking the agent's word):**
      - `jsa/pipeline/orchestrator.py:839` `_handle_session_expired` uses **two separate transactions** — `mark_failed` commits (`failed`, `retry_count=0`), then a second `async with` re-reads and soft-resets to `retry_count=1`. The intermediate state is durably observable.
      - `tests/backend/test_orchestrator.py` `_poll_job_state` waits for `state == failed` — a predicate that matches that transient intermediate commit as well as the intended terminal one.
      - `Orchestrator.run()` is `while not self._stopping: ... await self.wakeup.wait()`; it **never awaits `self._tasks`**. `_run_orchestrator_until`'s `finally` sets `_stopping` and returns while `_run_one` may still be mid-flight.
      - The agent's causal probe (delay injected *inside the window only*, no logic change): `DELAY=0.0` → `retry_count=1` (pass); `DELAY=0.25` → `retry_count=0` (the exact observed failure).
      - Same drain gap plausibly explains the second flaky test: `test_vanished_job_bailout_releases_the_slot` patches the **module-global** `repo.get_job`, and its `deleted["done"]` one-shot flag is consumed by the *first* caller after patching — a leaked, never-awaited `_run_one` task from an earlier test can eat the "vanish". It also calls `task.cancel()` without awaiting. Consistent, not separately confirmed.
      **=> This is a pre-existing latent test-harness defect, not a regression from this plan.** Fixing it means touching `test_orchestrator.py`'s shared helper or `jsa/pipeline/orchestrator.py` — both outside the plan's scope, and the assertions themselves are the check and must not move. **Step 6 halt: escalated to user with three options. Nothing dispatched until answered.**
      Correction to my own constraint on the agent: forbidding all edits to `test_orchestrator.py` was too broad. The *assertions* are the check; `_run_orchestrator_until` is a shared harness helper, and fixing its synchronization softens nothing.
      Post-fix gate revised — 3 green runs cannot distinguish "fixed" from "lucky" at a ~50% base rate. Any fix must be gated on the **delay-injection probe first** (`DELAY=0.25` inside the window must no longer flip the outcome — deterministic), then N green full runs.
      All probe files removed; baseline worktree verified clean (`git status --porcelain` empty).

---

00:15 (2026-09-04) **User authorized Option A (harness drain).** Also: Phase 6 check approved;
      claude-stat #97 resumed (`claude-stat approve --task 97`); further implementation to be
      delegated via `/opencode-delegate`, falling back to Claude subagents when the
      opencode-go limit is hit.

00:25 **Option A implemented — and A alone turned out to be insufficient. A+B was needed.**
      What I applied, all test-harness only (`jsa/pipeline/orchestrator.py` is byte-for-byte
      pristine — `git status --porcelain jsa/pipeline/orchestrator.py` empty):

      A) `_drain_inflight(orch)` added to `tests/backend/test_orchestrator.py` and
         `tests/backend/test_integration.py`, called in `_run_orchestrator_until`'s `finally`
         after the run task is awaited. `run()` never awaits `self._tasks`, so a `_run_one`
         could outlive its own test. Waits, then cancels+reaps whatever is still parked.
         `tests/backend/test_orchestrator_throttling.py` got the same treatment as
         `_shutdown(orch, task)`, replacing 6 bare `task.cancel()` teardowns.

      B) `_poll_job_state` gained an optional `settled` predicate, threaded through
         `_run_orchestrator_until`. `test_auto_resets_once_then_fails_permanently` now waits on
         `JobState.failed` AND `retry_count >= 1`. **Assertions are byte-identical** — only the
         wait condition narrowed.

      Why A alone failed: draining let the soft-reset commit land, so the row read `pending`
      instead of the intermediate `failed` — the poll had still ended on the FIRST expiry's
      transient commit, so the orchestrator was stopped before the second dispatch ever ran.
      The drain fixes the leak; the predicate fixes the ambiguous wait. Both are real.

      **Gate: the delay-injection probe, not run-counting.** A `0.25s` sleep injected between
      `_handle_session_expired`'s two commits (`orchestrator.py`, PROBE lines, since removed):
        - probe 0.25 + A+B fix  → 2 passed
        - probe 0.25 + no fix   → FAILED (deterministic control — the probe discriminates)
        - probe 1.0  + A+B fix  → 2 passed (stress)
      This is the deterministic evidence the earlier N-green-runs approach could not give.

00:40 **Full suite 6/6 green** — `1844 passed, 22 skipped, 0 failed` every run (~25s each).
      Note the count shape differs from the recorded baseline (`1779 passed, 2 skipped,
      21 deselected`) because this invocation omits `-m "not integration"`, so the 21
      integration tests are collected and self-skip instead of being deselected. 1844 =
      1779 baseline + 44 (Phase 1) + 20 (Phase 3) + 1. Superset of the baseline, 0 failures.
      Confirmed `pytest-randomly` is NOT installed, so collection order is deterministic
      alphabetical — the exact variable the earlier experiments isolated.

00:45 **Committed on `feat/prompt-injection`** (working tree clean afterwards):
        52f420a  test(orchestrator): drain in-flight _run_one tasks + terminal-failure wait
        4003b42  feat(injection): per-job prompt injection storage and API      (Phase 1)
        7cd994d  feat(injection): global preset ("dose") library store and routes (Phase 3)

00:50 **B1 review dispatched** — Phases 1+3 plus the harness commit, ONE medium pass, Sonnet
      (CLAUDE.md: code review is always Sonnet). Note: `/code-review` is not invocable from
      this session — the installed plugin command is PR/`gh`-shaped and needs a GitHub PR,
      and the `ultra` variant is user-triggered only. Ran the equivalent as a Sonnet subagent
      with fix authority instead, with explicit hard constraints: never weaken/skip/xfail a
      test or widen a timeout (escalate instead), never delete an untracked file, never
      stash, no git operations, don't touch orchestrator.py.

00:55 **Phase 2 plan re-verified against live source before dispatch** — every line reference
      in the plan is still exact: `assemble_system_prompt` at prompt_assembly.py:207; call
      sites stages.py 771 / 893 / 1190; `_build_initial_user_msg` at 1464. (`_load_history`
      is at 1507, plan says 1517 — harmless drift.) `parse_injection` and
      `PromptInjection.normalized()` both exist from Phase 1. Checked the two additional
      `language=language_code` sites at stages.py:940 and :1013 — they are `_self_heal_final`
      and the follow-up handler, both operating on an already-open session handle, NOT
      assembly sites. The plan's "exactly three call sites" is correct.
      Phase 2 dispatch is HELD until B1 drains and its checks re-run (babysit Step 4.5).

01:10 **B1 review landed — clean, zero fixes applied.** Verified personally rather than taking
      the report: `git status --porcelain` empty, `git diff HEAD` over the three harness files
      empty, HEAD still 7cd994d. So `_drain_inflight` / `settled` were NOT edited by the
      reviewer — no Step 6 "a fix that's more than a fix" halt needed.
      Reviewer confirmed against CLAUDE.md: presets store mirrors preferences.py /
      backend_models.py; `extra="forbid"` gives 422; engine.py's additive ALTER TABLE is
      idempotent; `PUT /api/jobs/{id}/injection` correctly does a plain field write rather
      than `repo.checkpoint` because it performs no state transition; presets PUT is a
      whole-list replace so there is no read-modify-write clobber window.

      One awareness item it raised, worth keeping: two pre-existing direct-`orch.run()` sites
      in `test_orchestrator.py` (`TestSemaphoreConcurrencyLimit`, `TestKickUnblocksLoop`,
      ~317/~353) still use a bare `asyncio.wait_for(task, ...)` with no drain. Untouched by
      this diff and not part of the demonstrated race — but they are exactly the shape that
      leaks a `_run_one`, so the leak detector below is aimed straight at them.

01:12 **Advisor caught a real gap in my own evidence, recorded so it is not lost.** The delay
      probe only exercises test 1's mechanism (`_handle_session_expired`). Tests 2 and 3 fail
      by a *different* mechanism (a leaked `_run_one` consuming throttling's one-shot
      `deleted["done"]` flag). For those, 6 green runs against the observed ~14% base rate is
      p ≈ 0.86^6 ≈ 0.40 — a coin flip, NOT a gate. That is the same weak-evidence error I
      already retracted once. Replaced it with a deterministic property check: a scratch
      autouse fixture that fails any test leaving a live `_run_one`/`Orchestrator.run` task
      behind. Run pre-fix and post-fix. Scratch file only — deleted after, never committed
      (`tests/` had no pre-existing conftest.py, so nothing to merge or restore).

01:13 Also confirmed `test_integration.py::TestHappyPath::test_both_jobs_approved_with_pdfs`
      genuinely PASSES rather than being one of the 22 skips — so test 3 really is covered by
      the green runs. All 22 skips are live-API tests under `tests/backend/integration/`
      self-skipping on absent API keys, i.e. exactly the set `-m "not integration"` used to
      deselect. The two measurements are therefore comparable in substance; re-running with
      `-m "not integration"` from here on so the ledger records ONE series, not two.

01:25 **CORRECTION to the 00:40 entry — "6/6 green" is NOT the whole picture. Do not read
      that line alone.** That series used bare `pytest -q`. Re-running the same suite as
      `pytest -q -m "not integration"` (the baseline-comparable invocation) produced
      `1 failed, 1842 passed, 2 skipped, 21 deselected`. The failing nodeid was lost because
      the capture grepped only `passed|failed` and not `^FAILED` — being re-run to identify
      it. **B1 is NOT drained and Phase 2 stays held until that test is named.**

01:26 **Leak detector results — the fix is real but INCOMPLETE.**
        pre-fix  (harness @ e9c278a): 4 errors
        post-fix (harness @ HEAD):    1 error   -> ['Orchestrator._run_one']
      The pre-fix arm also printed the smoking gun for the cross-test mechanism: leaked
      `_run_one` tasks from earlier tests hitting `(sqlite3.OperationalError) no such table:
      jobs` and `no active connection` — i.e. running against an engine a previous test had
      already disposed. Those are gone post-fix. So the drain eliminated cross-test pollution
      at every site that produced it, and one straggler remains.
      Open question that decides whether the straggler matters: does that leaked task survive
      into a LATER test's event loop, or is it a same-test straggler reaped when the loop
      closes? Discriminator: whether its test appears in the pre-fix `no such table` /
      `no active connection` list. A same-test straggler buys nothing for the three affected
      tests and is not worth chasing.
      Explicitly NOT doing: adding an autouse cleanup fixture to `tests/backend/conftest.py`.
      That would be a new global teardown across ~1800 tests, in a repo that deliberately has
      no conftest at all, to chase a leak not yet shown to cross a test boundary. That is a
      Step 6 scope decision for the user, not something to fold into "drain the harness".
      Scratch detector conftest deleted; `git status --porcelain` verified empty.

01:45 **The `1 failed` is identified — and it is a THIRD pre-existing race, in a file nothing
      in this plan touches.** Nodeid:
      `tests/backend/test_dev_tunnel.py::TestStartTunnelUrlPrinted::test_daemon_thread_is_started`
      (reproduced 1 of 3 runs). NOT one of the three originally-affected tests.

      The test snapshots `threading.enumerate()`, calls `_start_tunnel(8765)` with
      `mock_proc.stdout = iter([])`, then asserts the set difference is non-empty. The watcher
      thread iterates an EMPTY iterator, so it is designed to exit essentially immediately —
      the test is asserting that a thread built to finish at once is still alive at an
      arbitrary later moment. Under scheduler pressure it loses that race.

      **Proven pre-existing on the pristine baseline worktree** (`e9c278a`, zero plan code,
      zero harness change), same delay-probe method as before:
        as-is, isolation, 5 runs                                    -> 5/5 PASS
        + `time.sleep(0.05)` between `_start_tunnel()` and snapshot -> 3/3 FAIL
      Baseline restored afterwards; `git status --porcelain` empty.

      This is a genuinely-wrong check, so per the babysit rule it is a Step 6 halt, NOT
      something to fix silently. Not touched. Proposed corrected check is in the message to
      the user. It is unrelated to any phase's own check and does not block Phase 2.

01:50 **Closed the two remaining undrained `orch.run()` sites** the reviewer flagged, in
      `test_orchestrator.py` — same one-line `await _drain_inflight(orch)` already approved
      and applied elsewhere in the same file, no new mechanism:
        `TestSemaphoreConcurrencyLimit::test_at_most_max_parallel_jobs_running_simultaneously`
        `TestKickUnblocksLoop::test_kick_causes_newly_runnable_job_to_be_processed`
      `_drain_inflight` call sites now at lines 208, 373, 409. Re-running the leak detector to
      confirm 4 -> 1 -> 0.

02:00 **Leak detector after closing both sites: 4 -> 1 -> 1, but the survivor is now a
      DIFFERENT site** — `test_fit_assessment.py::TestOrchestratorFitBackendWiring::
      test_timeout_from_fit_backend_switches_to_next_backend_not_unfit`. The detector is
      revealing a whole family: bare `orch.run()` teardowns also exist in
      test_bf18_limit_detection.py, test_cv_source_of_truth.py, test_dev_autoanswer.py and
      test_log_events_bf7.py (~15 sites across 6 files, from the earlier grep).

      **Stopping the whack-a-mole here, deliberately.** The authorized scope was to fix the
      flake affecting the three demonstrated tests, and that is done:
      `TestGoogleSessionExpiredAutoRecovery` + `TestSlotReleasePaths` + `TestHappyPath`
      run **5/5 green** (12 passed each). The remaining leaks are pre-existing latent debt
      that the detector exposes but which are not currently failing anything. Chasing them
      would mean either touching 6 more files or adding a global autouse conftest — the
      latter being the Step 6 scope decision already flagged at 01:26. Recorded in the
      commit message so it is not lost.

02:05 Committed the two extra drain sites. `git diff` was exactly two added lines
      (`+ await _drain_inflight(orch)` twice) — no assertion touched.

02:15 **Orchestrator flake family CLOSED for the three affected tests.** Full suite 3x after
      c68a179: green / green / `1 failed` — and the one failure was
      `test_dev_tunnel.py::...::test_daemon_thread_is_started` every time, i.e. the
      pre-existing unrelated race from 01:45, NOT any orchestrator test. Targeted re-run of
      the three originally-affected tests: 5/5 green.
      Residual risk recorded honestly: the dev_tunnel race is still live and unfixed pending
      the user's A/B/C answer, so any single full-suite run has roughly a 1-in-3 chance of one
      red line that has nothing to do with this plan.

02:20 **Phase 2 dispatched to OpenCode** — `opencode-go/kimi-k2.7-code`, `--dir` pinned at the
      worktree (per the skill: inherited cwd is the documented headless hang mode). Chose the
      MEDIUM-tier escalation over the cheaper `longcat-2.0` default deliberately: Phase 2's
      load-bearing invariant is ONE injection resolution threaded to all THREE assembly sites,
      and silently dropping one is exactly LongCat's documented failure mode (drops
      imports/exports, forgets repo conventions). Relevant context (prompt_assembly.py + three
      stages.py regions + two gate test files) fits well under kimi's ~250K ceiling.
      Prompt is at scratchpad/phase2_prompt.txt. It carries explicit hard constraints: do not
      edit test_prompt_assembly.py or the existing tests in test_prompt_prefix_stability.py
      (byte-identity gates), never weaken an assertion, no git commands at all, do not touch
      orchestrator.py, do not touch `_build_fit_user_msg` (locked decision #2).
      Verification is MINE when it returns — its own reported pytest lines are not evidence.

02:55 **OpenCode dispatch STALLED — fell back to a Claude subagent, per the user's standing
      instruction.** Diagnosis, so this is recognizable next time: the run did NOT error and
      did NOT exit. `opencode run` (PID 2393) sat at 35:49 elapsed with only 0:48 CPU, and
      0.09s of CPU across a 5s sample — blocked, not working. stderr was 0 bytes. The last
      JSONL event was a `step_finish` with `reason: "tool-calls"` timestamped 34 minutes
      earlier, i.e. the model had asked for another tool call and nothing ever came back.
      It completed only 5 `read` calls (prompt_assembly.py, schema/injection.py, and the
      three test files) and never reached CLAUDE.md or stages.py. Zero writes — worktree
      verified clean after the kill, so nothing partial landed.
      This is what the opencode-go limit looks like from the outside: a silent hang, not an
      error event and not a nonzero exit. Cost recorded in the transcript: $0.0416, 42473
      tokens. Session `ses_f95880460ffeKvAOnfV6u0BJvP` preserved (sessions are durable in
      ~/.local/share/opencode/opencode.db) if it is ever worth resuming with `-s`.
      SIGTERM was ignored; needed SIGKILL.

02:58 **Phase 2 re-dispatched to a Claude subagent on Opus 5** — matching Phase 1's routing,
      since the plan names no model for Phase 2 and this is the crux. Same spec, same hard
      constraints, plus one addition the OpenCode prompt lacked: the agent is told that
      `test_dev_tunnel::test_daemon_thread_is_started` is a KNOWN pre-existing unrelated
      flake, that only-that-failing is expected, and that it must not investigate or fix it —
      otherwise it would burn its budget chasing my open Step 6 item.

03:10 **Phase 2 landed and VERIFIED PERSONALLY (not from the agent's report).** Commit 74797ab.
      Structural verification I ran myself:
        - `test_prompt_assembly.py` does not appear in `git diff --stat` at all
        - `git diff --numstat` on both appended test files: `395 0` and `89 0` — zero deletions
        - `grep -rn "assemble_system_prompt(" jsa/` -> exactly 4 hits: the def plus 785 / 910 /
          1215. No fourth, unthreaded assembly site exists.
        - `parse_injection(job.injection)` appears exactly ONCE, stages.py:742
        - `injection=injection` at 791 / 915 / 1221, matching those three sites
        - `_build_fit_user_msg` still called unchanged (locked decision #2 honored)
      The two load-bearing details are both correct: the wrapper is applied FIRST so every
      machine-authored section still lands after the postfix, and the `for_resume=True`
      sentinel early return returns `base`, not `prompt_text`. The agent's regression test for
      that return deliberately drives a SENTINEL-mode fake — a structured-capable fake takes
      the other branch and would pass even with the bug present. Good instinct, worth keeping.

      My own check runs (not the agent's):
        gates (test_prompt_assembly + test_prompt_prefix_stability) -> 131 passed
        test_prompt_injection.py                                    -> 76 passed (was 44)
        full suite -m "not integration"                             -> 1890 passed, 0 failed
      dev_tunnel did not trip on this particular run — it is still unfixed, still ~1-in-3.

03:12 One real defect found by me and fixed before committing: the `assemble_system_prompt`
      docstring still said `for_resume=True` "returns ``prompt_text`` unchanged". That is the
      sentinel path, which now returns `base`. Left stale, it reads as licence for a future
      agent to "fix" the code back to `prompt_text` and silently reintroduce the
      injection-drops-on-resume bug — exactly the failure class CLAUDE.md's "do not simplify
      X back to Y" notes exist to prevent. Rewrote it to state the invariant and why.
      Re-ran the three prompt test files after the edit: 207 passed.

03:15 **B2 review dispatched — Phase 2 alone, MEDIUM (not high), Sonnet.** Deliberate
      departure from the pre-registered "B2 = high" batch plan, for two converging reasons:
      the user asked to take it easy on reviews, and CLAUDE.md says to avoid `high`+ for a
      routine post-implementation pass and reserve it for when the user explicitly asks for a
      deeper review. Neither has happened. The review still gets Phase 2 on its own rather
      than bundled, because it is the crux and genuinely high blast radius — that part of the
      batch plan stands. Told the reviewer about the dev_tunnel flake so it does not burn its
      budget on my open Step 6 item.

03:30 **CORRECTION to the 02:15 entry — "orchestrator flake family CLOSED" was WRONG.**
      Do not read that line. The B2 reviewer reported
      `test_orchestrator_throttling.py::TestSlotReleasePaths::test_vanished_job_bailout_releases_the_slot`
      failing 2 of 4 runs, contradicting my claim. I re-measured with 6 sequential full-suite
      runs on an otherwise idle box (the reviewer's runs overlapped mine, a real
      CPU-contention confound — but that turned out not to be the explanation):
        runs 1,2,3,5,6 -> 1890 passed
        run 4          -> **4 failed**:
            test_integration.py::TestHappyPath::test_both_documents_written
            test_integration.py::TestHappyPath::test_both_jobs_approved_with_pdfs
            test_orchestrator.py::TestAwaitingInputResume::test_answered_followup_job_completes
            (+1 more — my `tail -4` truncated the list; re-capturing)

      **Why my 02:15 claim was bad evidence, stated so the mistake is not repeated:** I ran
      the three affected tests TARGETED, as three classes in isolation, and got 5/5 green.
      A targeted run structurally CANNOT reproduce cross-file collection ordering — and
      cross-file ordering is the entire mechanism of this defect. It was the wrong instrument
      for the question, not merely a small sample. Same category of error as the 00:40
      "6/6 green" entry: measuring something adjacent to the thing being claimed.

      New information in the data: the failures now arrive as a CLUSTER (4 in one run, across
      3+ files) rather than singly. That points at a cascading leak — one leaked task
      poisoning several subsequent tests — not an isolated site. It also means per-run
      failure probability is roughly 1 in 6 here, so any small number of green runs remains
      near-worthless as evidence.

      Consequence: the whack-a-mole I stopped at 02:00 was stopped too early. Enumerating
      ALL leaking sites with the detector rather than guessing the next one.

03:40 **METHODOLOGY CORRECTION — the leak detector is NOT deterministic, contrary to what I
      called it at 01:26 and repeated since.** Two consecutive full-suite runs with the
      detector installed just returned ZERO leak errors, after earlier runs of the same
      detector found 4, then 1 (semaphore), then 1 (fit_assessment). The fixture inspects
      `asyncio.all_tasks()` at teardown, so it only catches a leak if the leaked task happens
      to still be pending at that instant — the same timing sensitivity as the bug itself.
      **A zero result from it does NOT mean zero leaks.** It is a useful positive detector
      (a hit is real) and worthless as a negative. I over-claimed it as a deterministic gate;
      the delay-injection probe from 00:25 remains the only genuinely deterministic
      instrument used in this run.

      Switched to STATIC enumeration, which actually is deterministic — grep every test file
      that does `asyncio.create_task(orch.run())` and check whether it has any drain:

        file                              spawns  drains
        test_orchestrator.py                4       4     <- done
        test_orchestrator_throttling.py     6       7     <- done
        test_integration.py                 1       2     <- done
        test_bf18_limit_detection.py        2       0     <- UNDRAINED
        test_cv_source_of_truth.py          3       0     <- UNDRAINED
        test_dev_autoanswer.py              3       0     <- UNDRAINED
        test_fit_assessment.py              1       0     <- UNDRAINED
        test_log_events_bf7.py              3       0     <- UNDRAINED

      12 undrained spawn sites across 5 files. Every one of those files sorts alphabetically
      BEFORE test_integration.py / test_orchestrator*.py, so a task leaked in any of them is
      positioned to poison exactly the tests observed failing in the 03:30 cluster.
      This is the complete list — no more guessing the next site one at a time.

04:20 **CORRECTION — the leak hypothesis is refuted. Option C would fix nothing.**

Two orientation facts found before writing any Option C code:

1. `.venv/bin/pytest` is the real interpreter. Bare `pytest` in this shell is
   /opt/homebrew/bin/pytest with NO pytest-asyncio — it produces 12 ERRORS on
   test_orchestrator.py. All prior ledger figures are consistent with the venv,
   so past data stands, but every future measurement must pin `.venv/bin/pytest`.
2. `asyncio_default_test_loop_scope=function` (pytest-asyncio 1.4.0). A fresh
   event loop per test, closed at teardown. **A leaked task cannot execute inside
   a later test's loop.** This is the load-bearing beam of the "leaked task in an
   alphabetically-earlier file poisons test_integration" story — and it does not hold.

Signal I had already recorded and read past: the ONLY failing run (run 4) took
**38.02s**; the five green ones took 24.4–30.7s. Every named failure was a polling
test with a fixed wall-clock budget. I was dispatching OpenCode + subagents
concurrently during that window.

**Measurement A — 8 sequential full runs, nothing else dispatched:**
  8/8 green, 24.4–28.3s, zero failures. (Not decisive alone: P(8 green | p=1/6) = 0.23.)

**Measurement B — deliberate CPU contention (20 burners on 10 cores), 1 run:**
  `13 failed, 1877 passed in 87.02s`
  The 13 are a SUPERSET containing every test previously seen flaking:
  test_integration TestHappyPath::test_both_documents_written /
  ::test_both_jobs_approved_with_pdfs, test_orchestrator
  TestAwaitingInputResume::test_answered_followup_job_completes,
  test_orchestrator_throttling TestSlotReleasePaths, test_dev_tunnel.

**Exception types (the traceback I had never once read across many turns):**
  - 4x TimeoutError from the polling helpers — "did not reach JobState.review
    within 5.0s (current state: JobState.running)"
  - 3x AssertionError, same cause one layer up (job still running/cv_review)
  - 1x test_dev_tunnel — the pre-existing check defect already proved on baseline
  - 1x sqlalchemy OperationalError "cannot commit transaction - SQL statements in
    progress" (aiosqlite connection thread) — in test_integration.py, a file that
    ALREADY has drains, so not a drain-absence symptom
  - 1x sqlalchemy NoResultFound at stages.py:1005 `fu_result.scalar_one()`

**Conclusion: the flake family is wall-clock timeout sensitivity under CPU
contention, not asyncio task leaks.** The contention was self-inflicted — my own
concurrent subagent/OpenCode dispatching during measurement runs. Option C
(autouse drain + fail-loudly conftest) addresses none of the 13 observed failures.
HALTED before writing it; premise does not match the repo (Step 6).

Still standing from earlier work: 52f420a's `settled` predicate is a genuine fix,
proven independently by delay-injection (0.25s probe: fix->pass, no-fix->fail).
That was a real two-transaction race making a transient `failed` row observable.
Its value does not depend on the leak hypothesis. The drain half of that commit is
unproven hygiene, harmless, and stays.

**New debt surfaced (NOT fixed, out of plan scope):**
  - stages.py:1005 `scalar_one()` on the unanswered FollowUp assumes no one answers
    between the checkpoint commit and this SELECT. If a user answers via the API in
    that window, zero rows -> NoResultFound -> job hard-fails. Narrow but
    user-reachable, not dev-only.
  - test_dev_tunnel check defect — still awaiting user decision (open since 02:00).

04:35 **Audited my own halt — nothing was actually blocking.** Option C needs no
approval to NOT build; test_dev_tunnel is pre-existing red proved on the pristine
baseline and gates nothing; aiosqlite was my own raised question. The stated reason
for holding Phase 4 ("suite red-lines 1 in 6") dissolved with the 8/8 idle green.
Halting on nerves, not on a decision. Resumed.

04:36 Frontend baseline captured on the venv-independent side:
      `cd frontend && npm test` -> **24 files, 342 tests passed**, exit 0.

04:37 OpenCode probe: `opencode run "Reply with exactly: OK"` on longcat-2.0 hung
      past 60s with empty stderr — same silent-hang signature as the 35-minute
      hang at 01:xx. opencode-go limit still active. Killed at 60s (cost: 1 min,
      vs 35 last time). Fell back to a Claude Opus 5 subagent per the user's
      standing instruction ("use opencode delegate but also default to your
      subagents [when] opencode go limit is reached").

04:38 Phase 4 dispatched (Opus 5 subagent, frontend-only). Check on return:
      `cd frontend && npm test` (>=342 passing, all green) + `npm run build` succeeds.
      Both to be re-run personally, not trusted from the report.
      Dispatch carried the duplicate-preset-`id` trap explicitly (id is not unique
      server-side; using it as both React key and chip-delete handle deletes the
      wrong chip).

04:50 **Phase 4 VERIFIED** (my own runs, not the subagent's report):
      `cd frontend && npm test` -> **25 files, 361 tests passed** (baseline 24/342;
      +1 file, +19 tests, zero pre-existing tests edited — `git diff --stat` shows
      no `__tests__` file among the 7 modified).
      `npm run build` -> exit 0, built in 1.04s. Chunk-size warning pre-existing.

      Independently checked, not trusted:
      - Branch: `feat/prompt-injection` @ 74797ab. **The subagent reported "branch is
        main" — that was WRONG.** Verified via `git rev-parse --abbrev-ref HEAD`.
        Nothing was on main; no harm done, but the report was unreliable here.
      - Scope: `git status --short` is frontend-only, 7 M + 2 ??. No Python touched,
        no deletions of pre-existing files.
      - i18n: 20 new `promptInjector.*` keys; zero hardcoded JSX text literals found
        by grep. Only PROMPT_INJECTOR / SAVED_DOSES stay literal — matches CLAUDE.md's
        HUD-abbreviation carve-out (`StateMeta.code` vs `.label`).
      - Duplicate-preset-`id` trap: genuinely closed. `onDelete(index)` +
        `filter((_, idx) => idx !== i)`, key `${i}-${p.id}`. Regression test seeds two
        presets both `id:"dup"`, deletes the SECOND, asserts "First" survives.
      - Backend contract claim verified at source: `injection` is in `_job_to_dict`'s
        BASE dict (`jsa/api/routes_jobs.py:113`), so `GET /api/jobs` carries it.

      **PLAN DRIFT FOUND (not blocking):** Phase 4 cites the syringe SVG at
      `JSA App Shell.dc.html:576`. That file is **535 lines** and contains **zero**
      syringe/injector matches. The real design handoff is
      `~/Downloads/prompt_injection_feature.zip` (the source named in claude-stat
      task #97). Subagent extracted it to the scratchpad, not the repo. Correct call.

      **OPEN GAP — `scripts/translate-ui.sh` NOT run.** My dispatch forbade touching
      generated `strings.<lang>.json`, so the 20 new keys exist only in English.
      `useT()` falls back English -> raw key, so nothing breaks at runtime, but
      CLAUDE.md requires the catalogs be in sync before shipping. It calls an external
      API (spends money / sends strings out) -> needs user approval, batched before
      completion rather than run unilaterally.

05:05 **Phase 5 — self-implemented** (small enough that a subagent round-trip cost
      more than doing it). ReviewPane.tsx tab-bar <a>, 2 i18n keys, 4 tests.

      **Caught a vacuous test by mutation-testing my own work.** With the
      `jobLink?.trim()` guard replaced by `true`, only ONE of the two negative
      tests failed. Cause: `<a href="">` is NOT exposed with role "link"
      (aria-query requires a non-empty href), so `queryByRole("link")` returned
      null under mutation and the empty-string test passed for the wrong reason.
      Switched both negative assertions to `queryByText`. Re-ran the mutation:
      **both now fail**; restored: both pass. The guard is genuinely covered.

05:08 **Phase 6 — documentation, self-written** (needed feature context I hold).
      CLAUDE.md: cross-job invariant narrowed with the 3-row reuse table +
      rejected two-block split; new "Per-job prompt injection" section.
      Every factual claim verified at source before writing it (queued-gate 400
      at routes_jobs.py:565+, normalized()->None, assembly order at
      prompt_assembly.py:274-279, single parse at stages.py:742 threaded to
      791/915/1221, fit carve-out via _build_fit_user_msg untouched).

      **Phase 6's own proof condition MET:** the two parity gates are unedited.
      `git diff --numstat e9c278a..HEAD` -> test_prompt_assembly.py: no diff at
      all; test_prompt_prefix_stability.py: **89 insertions / 0 deletions**.
      Strengthened, never edited.

05:10 **All checks green, personally run:**
      backend  `.venv/bin/pytest -q -m "not integration"` -> 1890 passed, 2 skipped
      gates    131 passed | feature suites 96 passed
      frontend `npm test` -> 25 files / 365 tests passed
      build    `npm run build` -> exit 0

      Commits: 7922a53 (P4), e3c686b (P5), 860bd60 (P6). Tree clean.

05:12 B3 review dispatched — Sonnet, medium --fix, bundled over Phases 4+5+6
      (74797ab..HEAD), per the user's "bundle, don't review per-phase".

05:30 **B3 review drained (Sonnet, medium --fix, Phases 4+5+6).** 3 fixes applied,
      1 design-level item reported-only. Diff read personally — small, in-scope,
      no test softened, no file deleted, no writing git command run.

      **The fixes arrived with ZERO coverage.** I reverted all three and the suite
      stayed 365/365 green. Correct fixes, but nothing would have stopped a future
      refactor undoing them. Added 4 regression tests and mutation-verified: with
      all three reverted, exactly 3 fail (one per fix); restored, 369 pass.

      1. store.ts:419 horizontal clamp inversion — `min(max(m,x), upper)` picks the
         NEGATIVE upper bound when vw < 424, sliding the panel off the left edge.
      2. store.ts:110 `injectorPanelHeight` reserved a flat 560px while the panel's
         real CSS cap is `maxHeight: 80vh` -> above 700px viewport the footer goes
         below the fold. **DEVIATES from the plan's "port the formula verbatim"** —
         accepted because the plan itself specifies BOTH 560 and 80vh and the two
         contradict above 700px. Reversible; flagged to the user.
      3. ReviewPane.tsx job-link had no scheme guard — a `javascript:` URI in the
         free-text CSV column would execute in the app origin on click.

      Reported-only (correctly not fixed): openInjector's clamp is a THIRD
      reimplementation alongside ScratchBuffer's `clampPos` and ChatBox's popup
      positioning. Consolidation is a design change -> user's call, not the
      reviewer's. Logged as debt.

05:33 **FINAL — all checks re-run by me AFTER the review's edits:**
      backend  1890 passed, 2 skipped, 21 deselected
      gates    131 passed; numstat still 89 insertions / 0 deletions
      frontend 25 files / 369 tests passed
      build    exit 0
      Commit dd225b3. Tree clean.

      REMAINING (not blocking, needs user): translate-ui.sh unrun (external API
      spend); manual smoke steps 1-8; test_dev_tunnel; stages.py:1005 race;
      clamp-helper consolidation; MR decision.
