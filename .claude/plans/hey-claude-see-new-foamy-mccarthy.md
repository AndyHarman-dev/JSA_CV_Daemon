---
status: Done
---

# Manual Job Launch + `--select-language` Boot Sequence

## Context

Today JSA auto-runs every scraped job the instant it's ingested: `upsert_job` creates jobs
in `pending`, and the orchestrator's `list_runnable_jobs` immediately picks up `pending`
jobs and starts `fit_assessment`. The user never gets a chance to choose an output language
before generation begins.

Design handoff (`.claude/designs/launch_button_plus_boot_sequence.zip`) introduces two
changes aimed at that root problem:

1. **Manual per-job launch (always on).** Fresh jobs sit parked in a top-most
   "Queued — Not Started" sidebar group until the user clicks **LAUNCH** on a row. Nothing
   auto-runs. A **Launch All** action on the group header launches every queued job at once.
2. **`--select-language` boot sequence (flag-gated).** When the CLI is started with
   `--select-language`, a full-screen language picker + terminal "boot log" animation appears
   before the dashboard, giving an earlier, first-run opportunity to set the language. Without
   the flag, the app goes straight to the dashboard and the language defaults to English —
   but jobs are *still* parked (manual launch is unconditional).

**Decisions (from interview):**
- **Per-job language snapshot at launch.** A `job.language` is frozen when LAUNCH is clicked
  (from the then-current global preference). Later global-preference changes never affect an
  already-launched job — its whole pipeline (fit → cv → cover letter) stays in one language.
- **Launch All** button on the Queued group header, in addition to per-row LAUNCH.

---

## Backend

### 1. New `queued` initial state (`jsa/db/models.py`, `jsa/pipeline/state_machine.py`)
Introduce `JobState.queued = "queued"` as the fresh-ingest state. This isolates the change to
the single ingest chokepoint and leaves **every** existing reset→`pending` flow (cancel,
failed-reset, dismissed-reset, crash-recovery) untouched — `pending` keeps its exact current
meaning ("launched, dispatching"). `queued` = "fresh, never launched".
- `ALLOWED[JobState.queued] = {JobState.pending, JobState.dismissed}` (LAUNCH → pending;
  also allow dismiss of a queued job).
- No state transitions *into* `queued` except creation. `list_runnable_jobs`,
  `recovery_sweep`, and `_next_stage_for` need **no change** (they key off `running`/`pending`/
  etc.; `queued` is simply never runnable).

### 2. Add `job.language` column (`jsa/db/models.py`)
`language: Mapped[str | None] = mapped_column(String(8), nullable=True)` — the per-job
snapshot, null until LAUNCH. Nullable so pre-existing jobs fall back to the global pref.

### 3. Ingest creates `queued` (`jsa/db/repo.py::upsert_job`)
Change the new-job insert `state=JobState.pending` → `state=JobState.queued`. Existing-job
update path is unchanged (a queued job stays queued; failed→nuclear-reset still → pending).

### 4. LAUNCH endpoints (`jsa/api/routes_jobs.py`)
Mirror the existing `ignore-fit`/`cancel` endpoint shape (fetch → validate state → checkpoint
→ re-fetch → publish `StatusChangedEvent` → `orchestrator.kick()`):
- `POST /api/jobs/{job_id}/launch` — require `state == queued` (else 400). Read the global
  preference (`preferences_store.load`), set `job.language = prefs.language`, transition
  `queued → pending` via `repo.checkpoint`, publish status change, kick.
- `POST /api/jobs/launch-all` — launch every `queued` job (same per-job logic in a loop),
  return the count launched. One `kick()` at the end.

### 5. Thread per-job language into the pipeline (`jsa/pipeline/stages.py::run_stage`)
At the language-resolution chokepoint (currently lines ~358–361), prefer the snapshot:
```python
language_code = job.language or "en"
if job.language is None and preferences_path is not None:
    language_code = (await preferences_store.read(preferences_path)).language
```
This automatically flows the snapshot into `_run_fit_assessment`, `cv_adjust`, `cover_letter`,
and the `_validate_final_content` / `_parse_structured` context (the per-language
`_not_a_cover_letter` guard). No other pipeline change needed. (A launched job is always
`pending`→`running` fit_assessment *after* `job.language` is set, so every stage sees it.)

### 6. `--select-language` flag plumbing
Chain: `Settings.select_language: bool = False` (`jsa/config.py`) → typer flag
`--select-language` in `jsa/cli.py::main` (set `overrides["select_language"]`) → expose as
`"select_language"` key in `GET /api/config` (`jsa/api/routes_meta.py`). The flag only tells
the frontend to show the boot gate; the backend does nothing else with it.

---

## Frontend

Rebuild note: the `jsa` CLI serves the **gitignored built** `jsa/static`, not live source —
`npm run build` is required for any of this to appear when testing via the CLI (see memory
`rebuild-frontend-bundle`).

### 7. Types & store (`frontend/src/types.ts`, `frontend/src/store.ts`, `frontend/src/api.ts`)
- Add `"queued"` to the `JobState` TS union; add optional `language` to `JobDTO`.
- `api.ts`: add `launch(id)` → `POST /api/jobs/{id}/launch` and `launchAll()` →
  `POST /api/jobs/launch-all`.
- Store: add `selectLanguageMode: boolean` (hydrated from `/api/config`'s `select_language`),
  plus transient `launchAnim: Record<string,'arm'|'exit'|undefined>` and boot-gate state
  (`bootStage: 'lang'|'boot'|'app'`, `bootLang`, `bootLines`, `bootPct`). Add `launchJob(id)`
  and `launchAll()` actions that fire the animation then call the API; the real
  `pending→running` flip arrives via the existing WS `status_changed` refetch — keep the
  client animation decoupled from the WS event (do not block one on the other).

### 8. Job row LAUNCH control (`frontend/src/components/JobList.tsx`, new `LaunchButton.tsx`)
- Add a new top-most group `{ labelKey: "jobList.queued", states: ["queued"] }` above the
  others, with a **Launch All** button in its `<h2>` header.
- In `JobRow`, when `job.state === "queued"` render the LAUNCH pill (gold `#F4CE4A`,
  `#06080B` text, Chakra Petch, play-triangle) in place of `<StatusBadge>`; clicking it runs
  the arm→dematerialize animation (150ms arm, 380ms blur/scale/fade) then `launchJob(id)`.
  Reuse the `SHELL_THEME` tokens (`T.a`, `T.btnRadius`) and the existing `Icon` set
  (`frontend/src/theme/Icon.tsx`) — add a bolt/play glyph if missing at equivalent 1.5px
  stroke weight. Otherwise `StatusBadge` renders as today.

### 9. Boot gate (new `frontend/src/components/BootGate.tsx`, mounted in `App.tsx`)
Render a `position:fixed; inset:0` overlay above the shell when
`selectLanguageMode && bootStage !== 'app'`. Three views per the handoff:
- **Language picker** (`bootStage==='lang'`): logo + wordmark "JSA // DAEMON", caption,
  wrapped pill row over the **full** `languages` catalog from the store (reuse the existing
  `LanguagePill` component / its selected-vs-neutral styling), "CONFIRM & BOOT" button. On
  confirm: `setLanguage(bootLang)` (persists via `PUT /api/preferences`) → `bootStage='boot'`.
- **Boot log** (`bootStage==='boot'`): header line, boot lines printed in order (copy from the
  handoff README, line 6 interpolates the selected language uppercased), blinking cursor,
  progress bar + percentage. **Timing must be elapsed-wall-clock derived, not tick-counted**
  (`pct = min(100, (now - start)/2300)`, derive visible line count from `pct`) so a throttled
  background tab still completes — this is the README's explicit bug warning. On 100%, hold
  ~500ms → `bootStage='app'` (unmount overlay, revealing the already-mounted shell).
- The picker renders in the *current* global language / English fallback (language isn't
  applied yet at that point).

### 10. i18n strings (`frontend/src/i18n/strings.en.json` + `scripts/translate-ui.sh`)
Externalize via `useT()` **only** the real UI chrome: `jobList.queued` ("Queued — Not
Started"), `launch.button` ("LAUNCH"), `launch.launching` ("LAUNCHING"),
`launch.launchAll` ("Launch All"), boot picker caption, "CONFIRM & BOOT". **Do not**
externalize the boot-*log* lines (`daemon.init() … OK`, etc.) — they are the intentionally
code-like HUD category CLAUDE.md says to leave as literals. Re-run `scripts/translate-ui.sh`
so locale catalogs stay in sync.

---

## Notable behaviors / non-goals
- **Existing DBs:** jobs already sitting at `pending` will auto-run once on next launch
  (pending stays runnable). Acceptable for a fresh feature; not a regression.
- Reset/cancel/JD-change flows keep going to `pending` (auto-run) — `queued` is ingest-only.
  The pre-existing CLAUDE.md-vs-`upsert_job` mismatch on "jd_hash changed → reset to pending"
  is out of scope.
- No confirmation dialog on LAUNCH (deliberate, per design). No auto-advance on the picker.

---

## Tests to add / update
- **Backend:** `queued` in `state_machine` allowed table + a forbidden-transition case;
  `upsert_job` now creates `queued` (update `test_repo`/`test_state_machine`/orchestrator/api
  fixtures that assert fresh==`pending`); new `test` for `/launch` + `/launch-all`
  (queued→pending, sets `job.language`, kicks); `run_stage` uses `job.language` over the file
  when set. Use `FakeAgentBackend`/`FakeRenderer` per CLAUDE.md testing conventions.
- **Frontend (vitest):** LAUNCH button renders only for `queued`; clicking calls
  `api.launch`; Launch All calls `api.launchAll`; boot gate shows only when
  `selectLanguageMode`; boot progress derives from elapsed time.
- Some of the 27 backend / 9 frontend failures on HEAD are **pre-existing debt** (memory
  `preexisting-test-debt`) — don't attribute those to this work.

---

## Verification (end-to-end)
1. `pip install -e .` (no new deps expected) then `pytest -v -m "not integration"`.
2. `cd frontend && npm test`, then **`npm run build`** (required — CLI serves built bundle).
3. Manual, **no flag**: `jsa --csv jobs.csv --cv resume.pdf` → all jobs appear in
   "Queued — Not Started"; nothing runs. Click LAUNCH on one → dematerialize → RUNNING badge;
   confirm generation ran in English. Click Launch All → the rest launch.
4. Manual, **with flag**: `jsa --csv jobs.csv --cv resume.pdf --select-language` → boot gate
   picker appears first; pick a non-English language → boot log animates ~2.3s → dashboard;
   `~/.jsa/preferences.json` shows the chosen code. LAUNCH a job → its `job.language` is the
   chosen code and output is in that language even if you switch the global pref mid-run.
5. `curl localhost:8765/api/config` → includes `"select_language": true`.

## Critical files
- Backend: `jsa/db/models.py`, `jsa/pipeline/state_machine.py`, `jsa/db/repo.py`,
  `jsa/api/routes_jobs.py`, `jsa/api/routes_meta.py`, `jsa/pipeline/stages.py`,
  `jsa/config.py`, `jsa/cli.py`.
- Frontend: `frontend/src/types.ts`, `store.ts`, `api.ts`,
  `components/JobList.tsx` (+ new `LaunchButton.tsx`), new `components/BootGate.tsx`,
  `App.tsx`, `i18n/strings.en.json`, `components/cv-editor/LanguagePill.tsx` (reuse).
