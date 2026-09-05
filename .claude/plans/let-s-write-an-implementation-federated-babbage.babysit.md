# Babysit ledger — .claude/plans/let-s-write-an-implementation-federated-babbage.md
Plan: CV Decks — many base CVs + per-job base-CV assignment
Worktree: /Users/wiam/VSCodeProjects/JSA/.claude/worktrees/cv-decks  (branch `feat/cv-decks`, from local main e9c278a)
Started: 2026-09-04

Routing: plan-named `opus` phases → self (confirmed Opus 5). Plan-named `sonnet` phases →
OpenCode headless (`opencode-go/kimi-k2.7-code`, MEDIUM tier) per the user's delegation
instruction; Claude subagents are the fallback on a failed/poor delegated run.

| # | Phase | Model | Check | Review | Status | Attempts |
|---|-------|-------|-------|--------|--------|----------|
| 0 | Worktree setup | self | worktree on `feat/cv-decks` @ e9c278a | — | verified | 1 |
| 0b | Parity oracle captured on main | self | `test_cv_decks_parity.py` @ main → 2 passed, 3 skipped | — | verified (commit 3351f37) | 1 |
| 1 | Deck store + config paths | sonnet sub | `pytest tests/backend/test_cv_decks.py -q` → 43 passed | B1 high ✓3+1 | verified (7d6f649, b37419e) | 1 |
| 2 | Deck HTTP API + config signal | sonnet sub | `pytest tests/backend/test_cv_decks_api.py -q` → 21 passed | B1 high ✓3+1 | verified (09b57b6, b37419e) | 1 |
| 3 | `Job.base_cv_id` + assignment | sonnet sub + self (DELETE seam) | `pytest tests/backend/test_job_base_cv.py -q` → 19 passed | B1 high ✓3+1 | verified (09b57b6) | 1 |
| 4 | Orchestrator resolution + gate | self (opus) | parity 5 passed · pipeline 13 passed · suite 1878 passed | high ✓5+2 | verified (c7da346, 2d849c5) | 1 |
| 5 | Editor: deck rail | self (editorStore) + opencode (DeckRail) | `npm test` (editorStore + DeckRail specs) | batch B2 | pending | 0 |
| 6 | Job row trigger + picker | opencode kimi-k2.7-code | `npm test` (JobList + BaseCvPicker specs) | batch B2 | pending | 0 |
| 7 | Docs, build, review | opencode kimi-k2.7-code | `npm run build` + `translate-ui.sh --check` | — | pending | 0 |

Review batches: **B1** = phases 1–3 (server-side surface), effort `high --fix`.
**Phase 4** reviewed solo, `high --fix` (parity gate, dispatch path — highest blast radius).
**B2** = phases 5–6 (frontend surface), effort `high --fix`.
Every phase's own check is still run personally the moment that phase lands; batching
only defers the reviewer.

## Log
- Gate passed. 7 phases + Phase 0; every phase names a runnable check. Plan-level
  Verification steps 4–7 are manual/live-server checks requiring the user's real ~/.jsa
  and a resume — those are handed back at the end, not claimed as verified.
- Anchors verified against the repo before starting: `orchestrator.py:288` gate /
  `:473` run_stage call, `server.py:277` cv_structure_path, `routes_meta.py:20`
  cv_structure_exists, `cv_structure.py` 59 lines, `IconName` lacks `pencil`/`star`,
  `JobList.tsx` queued branch, `editorStore.ts` 699 lines / `save()` at 670. Plan is
  accurate — no drift halt needed at start.
- Worktree created from local `main` e9c278a (shares merge base with `feat/prompt-injection`
  and `feat/revision-tool-use`, per the plan's collision map). `pip install -e .` deliberately
  NOT run in the worktree (global `jsa` hijack hazard).
- Design handoff extracted to scratchpad for Phase 5/6 reference (4 files; the two `.dc.html`
  are 88K/115K — relevant sections are pasted into the delegated prompts, the code is not).
- **Routing change, 2026-09-04**: OpenCode delegation abandoned before it produced anything —
  the user reported the weekly limit drained on the first dispatch. Run aborted with an empty
  working tree (verified: `git status` clean, 0-byte JSON output, no partial writes). Fell
  back to Claude subagents pinned to the plan's named models, which was the user's stated
  fallback. Ledger `Model` column below reads `sonnet sub` from here on, not `opencode`.
- Phase 0b (parity oracle) verified at HEAD == main: `test_cv_decks_parity.py` → 2 passed,
  3 skipped (the deck-store half correctly `importorskip`s until Phase 1). Goldens generated
  from real `main` behavior, committed as `3351f37` BEFORE any implementation edit.
- Baseline full suite at HEAD == main: `1781 passed, 5 skipped, 21 deselected`.
- Frontend baseline (worktree, after `npm install` — node_modules is NOT shared with the
  main checkout): `npm test` → **24 files, 342 passed**. Captured before Phase 5 starts.
- Interpreter: `/Users/wiam/VSCodeProjects/JSA/venv/bin/python` (3.14, has pytest-asyncio).
  Confirmed it resolves `jsa` from the WORKTREE, not the main checkout's editable install.

### Batch B1 review — `/code-review medium --fix main...HEAD`, drained 2026-09-04
Scope verified BEFORE accepting any finding (new global CLAUDE.md rule): the report named
`routes_cv_structure.py` / `routes_meta.py` / `cv_decks.py` on `feat/cv-decks`, matching the
17-file diff of 3351f37..09b57b6. Main checkout confirmed clean afterwards.

Findings 1 + 2 (high, NOT fixed — correctly out of scope): `PUT /api/cv-structure` now writes
a deck while `server.py:278` / `orchestrator.py:288` / `stages._read_base_structure` still
read the legacy `cv_structure.json`, and `cv_structure_exists` now disagrees with the
orchestrator's gate. Both are exactly what Phase 4 exists to close. **This branch must not
merge without Phase 4** — recorded here so that can't be forgotten if the run is interrupted.

Findings 3–5 (fixed by the reviewer, accepted after reading the diff — all additive, no
approach change, no files outside the phase's scope):
- `_index_lock` around all six index mutators (lost-update on overlapping create/save).
- `resolve_path` warns on a silent `deck_id=None` fallback off a missing default deck.
- `GET /api/config` pays one index read, not two (`resolve_path(..., index=)`).

**Defect found by my own regression test for finding 3, fixed in b37419e:** the reviewer's
locks were module-level `asyncio.Lock()` singletons. `asyncio.Lock` binds to an event loop
on its first *contended* acquire and raises `RuntimeError: bound to a different event loop`
for every contended acquire from any other loop after that. Production has one loop so it
would never fire there — but each pytest-asyncio test gets a fresh loop, so the first test
to genuinely contend a lock poisons it for every later one. Both locks are now keyed by
running loop through `_lock(name)` (`WeakKeyDictionary[loop, dict[name, Lock]]`).

Re-verification after the review + fix (the reviewer edited code already recorded green):
- `test_cv_decks.py` → 43 passed · `test_cv_decks_api.py` → 21 · `test_job_base_cv.py` → 19
- `test_cv_decks_parity.py` → 5 passed, 0 skipped; `git diff 3351f37` on the oracle and its
  goldens is **empty** — byte-untouched since capture on main.
- Full suite → **1863 passed, 22 skipped** (was 1861/22 pre-fix; +2 = the new lock tests).
- Mutation-tested both new tests: `_lock` returning a fresh lock each call → 3 failures
  (including the pre-existing migrate-lock test); restoring the module-level singleton shape
  → the second lock test fails with the exact RuntimeError. Neither test is a tautology.

### Phase 4 — orchestrator resolution + gate (self, opus) — 2026-09-04
Edited in an order that kept the suite green at every step (server factory → orchestrator
init/wrap → gate + dispatch → server construction site → cli + its one test).

- `_read_base_structure(None)` verified to return `None` cleanly *before* writing either
  new call site. Both the gate (zero decks) and dispatch (deck deleted between gate and
  `_run_one`) can pass it `None`; had it raised, the dispatch loop would have died silently.
- `grep -rn "_cv_structure_path" tests/` → nothing. The field could be dropped outright
  rather than kept assigned-but-unread.
- **`TestOrchestratorCvGate` passes unmodified, corrupt case included** — the wrap
  reproduces today's gate byte-for-byte for every caller that passes only a bare path.
- One intentional red, exactly the one the plan predicted and nothing else:
  `test_seeds_and_saves_when_missing`. Rewritten to assert the deck, plus a new assertion
  that the legacy file is **not** dual-written (the plan explicitly forbids that fix).
  Its three siblings passed unmodified, as predicted.

**Parity gate — PASSED.** `test_cv_decks_parity.py` → 5 passed, and
`git diff 3351f37 -- tests/backend/test_cv_decks_parity.py tests/backend/fixtures/` is
empty: the oracle and both goldens are byte-untouched since they were captured on `main`
before any decks code existed. A migrated legacy install reproduces the `fit_assessment`
and `cv_adjust` user messages byte-identically *through the real production seam*.

New `test_cv_decks_pipeline.py` (7 tests) asserts on the message the backend actually
received, never on `resolve_path`'s return value — the latter re-tests Phase 1 and would
pass even if `_run_one` never threaded the path through. Mutation-tested both seams:
dispatch ignoring `job.base_cv_id` → 3 failures; gate disabled → 3 failures (including
both pre-existing `TestOrchestratorCvGate` cases).

Review findings 1 and 2 from batch B1 are now closed **by evidence, not by reasoning**:
the gate-unblock test drives the save through the real `PUT /api/cv-structure` route, and
a second test asserts `/api/config`'s banner and the resolver agree before and after it.

**Known residue, deliberately not fixed:** `cv_structure_exists` is existence-only
(`resolve_path(...) is not None`) while the gate additionally requires *loadability*, so a
present-but-corrupt default deck shows "CV exists" in the banner while dispatch stays
blocked. Closing it means a loadability read on `/api/config`, which CLAUDE.md forbids on
the frontend boot path (raced against an 8s timeout). Same user-visible shape as the
pre-decks code had for a corrupt `cv_structure.json`; not a regression.

Suite after Phase 4: **1870 passed, 22 skipped** (was 1863 — +7 new pipeline tests).

### Phase 4 review — `/code-review high --fix 09b57b6..HEAD`, drained 2026-09-04
Scope verified before accepting anything: correct worktree, correct branch, main clean.
`frontend/package-lock.json` showed modified — that was **my own `npm install`** when
capturing the frontend baseline, not the reviewer's; reverted.

**Finding 1 was a genuine critical bug I introduced in Phase 4, and the review is right.**
The gate moved from `stages._read_base_structure` (which catches
`JSONDecodeError`/`ValidationError` by design and holds jobs `pending`) to
`resolve_path → load_index → json.loads`. `run()`'s loop body has no try/except and the
task is spawned with a bare `create_task`, so a crash-truncated or torn index killed
dispatch for the whole process lifetime while the server kept serving HTTP — no failed
job, no visible error, just a queue that never moves again. The advisor had flagged the
*shape* of this risk (`_read_base_structure(None)` tolerance) and I verified that half;
the index read one layer further down is the half I missed.

Fixes accepted after reading the diff (all narrow, none change the phase's approach):
gate-only `_base_cv_for_gate()` catching exactly those two exceptions; CLI guard + clean
exit 1; `--cv` seeds into the existing default deck; atomic `_save_index_sync`.

**Finding 7 fixed by me, deliberately going one step past the review's report-only call.**
It is not a new defect — it is finding 1's root cause on a third reader. `/api/config` is
on the frontend boot path (raced against an 8s timeout per CLAUDE.md), so hardening the
gate and the CLI and leaving this one raising yields a job queue that holds gracefully
behind a UI that never loads. Two-line guard, same exception pair, `_config_payload`
helper so both branches return an identical shape.

**Finding 6 deferred to Phase 5, on purpose.** `put_cv_structure`'s
`load_index → create_deck → save_deck` is a check-then-act above the index lock. Different
class (a lost write, not an unreadable state) and Phase 5's `save()` is specified to add a
*second* client-side path into the same check-then-act. Fixing the route now means fixing
it twice. **Phase 5 prerequisite: add `cv_decks.ensure_default_deck()` under the "index"
lock and route both callers through it.**

**Two defects found in the reviewer's own fixes — both caught by mutation testing, neither
by its green suite.** This is the strongest evidence in the run that "re-run every check
personally" is doing real work, not ceremony:
1. (b37419e) module-level `asyncio.Lock()` singletons, which bind to one event loop.
2. `TestAtomicIndexWrite` asserted only "final file parses + no temp litter" — which a
   plain `write_text` satisfies just as well. It pinned nothing. Replaced with a write
   that dies halfway and asserts the *previous* index is still readable; mutation-confirmed
   (reverting to `write_text` fails the new test and still passes the old one).

Mutation results this round: gate guard removed → its test fails; non-atomic write → new
atomicity test fails; `/api/config` guard removed → its test fails. All three real.

Re-verification after the review's edits: phase checks 102 passed · parity **5 passed,
oracle diff empty** · full suite **1878 passed, 22 skipped**.

## Plan defects found during the run (fixed in-flight, not halted on)
- **Phase 2's `cv_structure_exists` snippet is wrong as written.** The plan says
  `bool(cv_decks.resolve_path(settings, None))`, but `resolve_path` is async (Phase 1
  mandates it) — `bool(<coroutine>)` is unconditionally `True`, so the field would be true
  forever, which is exactly the lie Phase 2's own Problems section says it is fixing. The
  plan's *intent* is unambiguous ("at least one deck has a CV"), so this is a missing
  `await`, not a design question: fixed in the Phase 2 prompt, not escalated. `routes_meta.py`
  `config()` is already `async`, so the fix is one keyword.
- **First-boot migration race.** `/api/config` gaining `cv_deck_count` puts `load_index` on
  the boot path, and `load_index` triggers `migrate_legacy` (a WRITE) on a legacy install.
  The orchestrator's gate does the same on its first cycle — the two can race on the very
  first boot after upgrade and mint two decks from one legacy file. Guard requested in
  Phase 1: re-check `index_path.exists()` immediately before writing, under a module-level
  `asyncio.Lock`.

## Verification standard for each landing phase (not just "its own tests pass")
A phase's self-written tests only prove self-consistency — the same agent wrote both halves.
The independent checks run every time:
- full suite must stay at **>= 1781 passed**, and
- `test_cv_decks_parity.py` **skips must fall 5 -> 2** as the deck store lands.
  `importorskip` swallows an `ImportError` raised *inside* `cv_decks.py`, so a still-skipping
  parity test looks identical to "phase hasn't landed". If skips stay at 5, run
  `python -c "import jsa.store.cv_decks"` before believing any other green.

## Phase 1 — verification record (2026-09-04)
Independent re-runs, not the subagent's report:
- `import jsa.store.cv_decks` → OK (so `importorskip` is not masking an ImportError).
- `test_cv_decks_parity.py` → **5 passed, 0 skipped** (better than the 5→2 bar: the whole
  deck-store half now runs for real, i.e. legacy→deck migration parity is *proven*, not
  merely unblocked).
- `tests/backend/test_cv_decks.py` → 38 passed.
- Full suite → 1822 passed, 2 skipped (baseline was 1781+5; +38 new, +3 parity unskipped).

Structural checks the self-written tests could not catch (all pass):
- `cv_decks_dir` is referenced exactly once in code — inside `deck_path`. No function
  bypasses the `_DECK_ID_RE` traversal guard with an inline path join.
- Every filesystem call is inside a `_sync` helper behind `asyncio.to_thread`, **including
  `resolve_path`'s `.exists()`** (the likely miss — it reads like permission to call
  `.exists()` bare, and it runs in the orchestrator's hot loop).

### PRE-EXISTING FLAKE — not introduced by this work, not fixed here
`tests/backend/test_integration.py::TestHappyPath::test_both_jobs_approved_with_pdfs` fails
intermittently (~1 run in 3–5) under the full suite; passes in isolation every time.
**Verified pre-existing**: reproduced at commit `3351f37` (main + parity oracle only, zero
decks code) — 1 failure in 5 full-suite runs there. Recorded rather than "fixed": softening
or quarantining a test that was already flaky on `main` would be exactly the criteria-change
this run must not make. Flagged for the user as a separate pre-existing issue.

### Corrections sent back to the Phase 1 agent (not accepted as-is)
1. The first-boot migration race (see "Plan defects" above) was NOT guarded — my omission
   from the dispatch prompt, not the agent's. Requested a module-level `asyncio.Lock` with
   an `index_path.exists()` re-check *inside* the lock, plus a concurrent-`load_index`
   `asyncio.gather` test asserting exactly one deck and one file.
2. The agent's own flagged deviation — `ValueError` now meaning both "malformed id" (400)
   and "unknown id" (404) — is a real problem, but the fix belongs in the store, not the
   route: making Phase 2 re-derive the distinction by re-running `_DECK_ID_RE` would
   duplicate this module's validation rule in the API layer where it would rot. Requested
   `InvalidDeckId(ValueError)` / `UnknownDeckId(ValueError)` instead; both keep subclassing
   `ValueError` so existing catches and tests stay valid.
   The agent's other two deviations were accepted as correct (membership check before the
   file write, avoiding an orphan file; narrowed corruption catch).

## Phases 2+3 — verification record (2026-09-04), commit 09b57b6
Run in PARALLEL (disjoint file ownership: Phase 2 owned `jsa/api/routes_cv_decks.py`,
`routes_cv_structure.py`, `routes_meta.py`, `server.py`; Phase 3 owned `jsa/db/*`,
`routes_jobs.py`). Neither could write the one coupling point — `DELETE /api/cv-decks/{id}`
calling `repo.clear_base_cv_assignments` — because the route lives in Phase 2's file and the
helper in Phase 3's. **I wired that seam myself and wrote its test**, since a seam is exactly
where a silent gap hides: the helper was unit-tested, the route was unit-tested, and nothing
would have noticed the route never calling the helper.
- **Mutation-checked that test**: removed the wiring → `1 failed`; restored → `2 passed`.
  A wiring test that passes with the wiring deleted would have been worse than no test.
- `server.py` diff is exactly 2 lines (import + `include_router`) — ownership boundary held.
- Full suite → **1860 passed, 2 skipped**. Parity oracle byte-untouched throughout.

### Flake investigation (resolved — no action)
Mid-verification a run showed 3 failures (`test_dev_tunnel`, `test_integration`), which
looked alarming. Cause: I was running the suite in a second worktree concurrently. With the
machine uncontended the current tree is **5/5 clean at 1858 passed**, and no deck test ever
appeared in a failure. The earlier-recorded `test_integration` flake stands as pre-existing
(reproduced at `3351f37`), and is load-sensitive rather than order-sensitive — order was
pinned with `-p no:randomly` in every run.

### Accepted deviation (Phase 2)
`PUT`/`duplicate` call `cv_decks.deck_path()` before `save_deck`/`duplicate_deck` so a
malformed id 400s consistently with GET/PATCH/DELETE. Without it those two routes would 404
instead, because the store checks index membership before touching `deck_path`. Accepted:
it calls the store's own validator, it does NOT re-implement the regex in the route layer,
which was the thing worth preventing.

### Review effort — CLAUDE.md overrides the skill's default
The babysit blast-radius table would rate this batch `high` (DB migration + path-traversal
surface + HTTP API + state rules across 3 phases). CLAUDE.md's Code Review Rules explicitly
cap the routine post-implementation pass at `low`/`medium --fix` and say to reserve `high`+
for when the user asks. Running **`medium --fix`**, per CLAUDE.md.

---

### Phase 5 prerequisite — `ensure_default_deck` (closes Phase-4 review finding 6)

Committed `5b221ba`. `PUT /api/cv-structure` inlined `load_index -> if default_id is None:
create_deck`, a check-then-act *above* the index lock. Replaced with
`cv_decks.ensure_default_deck()`, which does the read and the mint under one hold of
`_lock("index")`. `_append_deck` split out of `create_deck` because `asyncio.Lock` is not
reentrant. Verified `migrate_legacy` takes a *distinct* `_lock("migrate")` and never
`_lock("index")`, so the nested-lock path introduces no deadlock.

Mutation-tested, both ways:
- return `default_id` without minting → 2 tests fail.
- **revert to the pre-fix check-then-act above the lock → 10 concurrent first-install
  callers return 10 DISTINCT deck ids.** Finding 6 reproduced live and now pinned.

Checks after the prereq, run before any frontend work (per advisor): phase checks 97 passed
· parity gate 5 passed · `git diff 3351f37 -- tests/backend/test_cv_decks_parity.py` empty.

### PLAN DEFECT (Phase 5) — `flushAndPersist` gated on a flag that means something else

The plan specifies: *"`flushAndPersist()` — `commit()`, then if `dirty` call `save()`"*.

`dirty` in `editorStore.ts` is **not** "unsaved relative to the server". It is the *typing-
coalescence* flag: "the buffer differs from the newest history snapshot". `commit()` always
clears it (`editorStore.ts:396`), and structural edits (add/delete/reorder section, entry,
bullet) call `commit()` synchronously inside `applyEdit`. So:

- After `commit()`, `dirty` is **always** false → the plan's literal `flushAndPersist` would
  never save anything, ever.
- A structural edit leaves `dirty` false immediately → switching decks right after deleting a
  section would **silently discard that deletion**. Data loss, no prompt.

Resolved by adding a real `unsaved: boolean` to the store (true on every `applyEdit` and on
`undo`/`redo`; false on `load`/`startBlank`/`reset`) and gating `flushAndPersist` on it.
No existing behavior changes — the COMMIT button's `save()` was and stays unconditional.

Not escalated as a halt: the two readings do not diverge on design, one of them is simply
broken. Flagged to the user in the phase report instead.

Mutation-confirmed: reverting the gate to `dirty` fails 4 tests, led by
*"persists a structural edit that left `dirty` false"*.

### Phase 5 — deck rail

Split per the plan's routing. **Mine (opus):** `api.ts` deck fns, `types.ts` `CvDeckDTO`,
`Icon.tsx` `pencil`+`star`, `strings.en.json` `cvDecks.*` keys, `editorStore.ts`,
`CvEditor.tsx` mount+render, `editorStore.test.ts`.
**Sonnet subagent:** `DeckRail.tsx` + `DeckRail.test.tsx` only, dispatched with a hard file-
ownership boundary and the landed store contract.

Plan drift, both benign and absorbed into Phase 5: the plan schedules `CvDeckDTO` and the
`cvDecks.*` i18n keys in Phase 6, but Phase 5's store and rail both need them, so they moved
up. `translate-ui.sh` fan-out stays in Phase 6 as planned.

Store mutations, all caught:
1. gate `flushAndPersist` on `dirty` → 4 fail (the plan-defect pin above)
2. route `duplicateDeck` through `flushAndPersist` → 1 fail (it must refuse, never prompt —
   the server copies the deck *file*, so a discard would duplicate stale disk content)
3. `save()` back to the legacy `saveCvStructure` alias → 7 fail

**Phase 5 verified — `fe0cb52`.** tsc exit 0 · frontend 25 files / **378 passed** (baseline
24 / 342) · backend phase checks **97 passed** · parity gate **5 passed** · oracle diff empty.

#### Two defects found in the subagent's DeckRail, neither visible to its own green suite

The Sonnet subagent reported 25 files / 373 passed, tsc clean, and it held its file boundary
exactly (empty unstaged diff against my staged files — verified, not taken on its word). Both
defects below survived that green suite.

1. **The live-label rule was dead.** `DeckRail` subscribed to seven store slices but not to
   `cv`, while `deckLabel()` reads `cv` through `get()` — which creates no subscription. So
   the plan's "the active row tracks the live buffer" rule rendered once and then froze.
   **Its tests could not have caught this: `seed()` replaces `deckLabel` with a stub that has
   no live-buffer behaviour at all.** Added a test using the real `deckLabel` (captured at
   module load, before `seed()` swaps it), watched it fail, then fixed the subscription.

2. **`stopPropagation` was pinned on one button out of four.** Its test
   ("clicking an icon button does not switch") only clicked `star`, on the *active* row.
   Dropping `stopPropagation` from `pencil`, `copy` or `trash` went undetected — so clicking
   DELETE could also switch decks underneath the user. Replaced with an `it.each` over all
   four, on the *non-active* row where a leaked click is a real navigation.

   Worth recording how this was nearly missed: my first replacement looped over the four
   buttons inside a single `it`, and it reported all four clean **while the trash mutation
   was live on disk**. A direct probe proved the leak was real (`switchDeck` called once).
   userEvent's pointer state persists across clicks within one test and was masking it. One
   `it` per button — fresh render, fresh userEvent — catches all four. The lesson is that a
   *newly written* test passing is not evidence it works either; only the mutation is.

Mutation results on the rail, all confirmed: trash-at-one-deck guard (1 fail), Escape
committing instead of cancelling (1 fail), inferring lock removed (1 fail), live-CV
subscription removed (1 fail), stopPropagation dropped per button (1 fail each, x4).

Known residue, deliberate: `newDeck()` registers an empty slot server-side before the user
saves anything, so abandoning it leaves a `has_cv: false` row in the index. That is the
plan's design (Phase 6's picker filters on `has_cv`), not a leak — it has an index entry.

**Still open for Phase 6:** batch B2 review (`high --fix`) covering phases 5+6, i18n
`translate-ui.sh` fan-out, and `npm run build` in Phase 7.

