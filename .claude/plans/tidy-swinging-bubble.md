---
status: InProgress
---

# Make `cv_structure` the single source of truth for CV content; demote `--cv` to optional bootstrap

## Context

Today two independent CV representations exist and never sync:

1. `--cv` (required CLI flag) → text extracted once at startup (`jsa/ingest/cv_loader.py`) and stamped onto every job row as `Job.cv_text`.
2. `cv_structure.json` (`jsa/store/cv_structure.py`, edited in the frontend Structure Editor) — validated, ordered `CVDocument`.

For a fresh `cv_adjust` session, `_build_initial_user_msg` (`jsa/pipeline/stages.py:960-992`) injects **both**: the structure JSON as an "authoritative skeleton" *and* the raw `CV TEXT:` block. That dual injection is why an agent asked the user which CV to use — the authority is only a soft prompt instruction. Worse, `fit_assessment` (`_build_fit_user_msg`, stages.py:657-667) uses **only** the stale `--cv` text and never sees structure edits — a real correctness bug: fit is judged on a CV the user may have substantially edited.

**Decisions (user-confirmed):**
- `--cv` becomes **optional, bootstrap-only**: if `cv_structure.json` doesn't exist and `--cv` is given, run the existing infer flow (`jsa/pipeline/infer_structure.py::run_infer`) once at startup to seed and save the structure; afterwards `--cv` is ignored (with a printed note).
- If no structure exists when jobs would run, **block**: jobs stay `pending` (never `failed`), UI shows a "set up your CV first" banner pointing at the Structure Editor. Saving the structure unblocks without restart.
- Raw `--cv` text is no longer injected into any prompt.

**Design choices:**
- `cv_adjust` prompt: structure **JSON only** (drop `CV TEXT:` block; no parallel markdown rendering — avoids duplicate tokens and competing representations).
- `fit_assessment` prompt: `cv_to_markdown(structure)` (`jsa/render/serialize.py`) under a `CV:` header — readable content, not JSON shape.
- `Job.cv_text` column: **keep but stop populating** (`# DEPRECATED` comment). Migration story is `create_all` + additive `ALTER TABLE` only (`jsa/db/engine.py`); existing DBs have `cv_text TEXT NOT NULL`, and ~20 test files construct `Job(cv_text=...)`. Keep `repo.upsert_job`'s insert default (`repo.py:62`), delete the update branch (`repo.py:76-77`).
- Blocking gate lives in `Orchestrator.run()`, active only when `cv_structure_path is not None` (production always passes it via `server.py:106`; tests passing `None` are unaffected — document this in the gate docstring).

---

## Phase 1 — Prompt assembly (`jsa/pipeline/stages.py`)

1. In `run_stage`, hoist the structure read above the fit-assessment early-return (currently at :466-469, fit branch at :374-378):
   ```python
   base_structure = None
   if stage in (Stage.fit_assessment, Stage.cv_adjust) and cv_structure_path is not None:
       base_structure = await cv_structure_store.read(cv_structure_path)
   ```
   Pass `base_structure` to both `_run_fit_assessment(...)` and `_build_initial_user_msg(...)`.
2. `_build_initial_user_msg` (:960-992): delete the `CV TEXT:\n{job.cv_text}\n\n` line (:989); update docstring (:970-975) — the structure **is** the base CV, no "soft steer"/fallback language. Result: `brief + skeleton(JSON) + JOB DESCRIPTION + TIER`.
3. `_build_fit_user_msg` (:657-667): new signature `(job, base_structure: CVDocument | None)`; replace `CV TEXT:\n{job.cv_text}` with `CV:\n{cv_to_markdown(base_structure)}` when present. `_run_fit_assessment` (:705) takes and forwards it.
4. Fix stale comments referencing raw `cv_text` behavior (:465, :974).

## Phase 2 — Prompt stubs (keep sentinel sections **exactly** intact — `test_scaffold.py:132` asserts them)

- `jsa/prompts/PROMPT_CDADJUST.md`: remove "base CV attached as file" / "when the block is absent, derive from base CV text" fallback language; state the `BASE CV STRUCTURE` JSON block **is** the base CV and authoritative skeleton (lines ~6-15, ~92-93, ~99-104, Phase-3 step 1).
- `jsa/prompts/PROMPT_FIT_ASSESSMENT.md`: line ~9 — "`CV TEXT` — …" → "`CV` — the candidate's current base CV (rendered from their curated CV structure)".

## Phase 3 — Blocking gate + API signal

1. `jsa/pipeline/orchestrator.py::run()` (loop starts :150): after `self.wakeup.clear()`, if `self._cv_structure_path is not None and not await asyncio.to_thread(self._cv_structure_path.exists)` → publish a one-shot bus `LogEvent` ("No CV structure — jobs stay pending until you set up your CV in the Structure Editor"; track a `_blocked_announced` bool, reset when structure appears), then `await self.wakeup.wait(); continue`. Jobs never leave `pending`.
2. `jsa/api/routes_cv_structure.py::put_cv_structure` (:49): after save, `orch = getattr(request.app.state, "orchestrator", None); if orch: orch.kick()` — saving in the editor unblocks without restart.
3. `jsa/api/routes_meta.py` `/api/config`: add `"cv_structure_exists": settings.cv_structure_path.exists()`.

## Phase 4 — CLI bootstrap (`jsa/cli.py`)

1. Extract the backend-factory duplicated in `jsa/server.py:85-95` into a shared `make_backend_factory(settings)`; use it in both `create_app` and the CLI bootstrap.
2. `--cv` → `Optional[Path] = typer.Option(None, ...)` (:86-93); guard the extension check (:110-113) with `if cv is not None`. Update help text: "used once to seed the CV structure if none exists; ignored afterwards".
3. `_preflight` (:192-217): delete `load_cv` call + `job_data["cv_text"]` stamping (:201, :209). After `init_db`, add `_bootstrap_cv_structure(settings, cv_path, backend)` (factored for direct testing with `FakeAgentBackend`):
   - structure exists + `--cv` given → echo "Note: CV structure already exists at <path>; --cv is ignored."
   - missing + `--cv` given → `run_infer(backend, cv_path, task_id="cli-bootstrap", publish=<async progress printer>)` → `cv_structure.save(settings, cv)`. On `InferError`: echo reason + `typer.Exit(1)` (explicit user intent — don't silently fall into the blocked state).
   - missing + no `--cv` → echo "No CV structure found — jobs stay pending until you set it up in the editor."

## Phase 5 — Frontend

Files: `frontend/src/store.ts`, `frontend/src/editorStore.ts`, `frontend/src/components/JobList.tsx`, `frontend/src/i18n/strings.en.json` (+ ConfigDTO type if present).

1. `store.ts`: `cvStructureExists: boolean | null` + setter; hydrate where `/api/config` is fetched (same place as `hydrateLanguage`/`configReady`).
2. `editorStore.ts`: on successful structure commit (PUT), flip `cvStructureExists` to `true` via a lazy `useStore.getState()` call (avoid import cycle — store.ts already imports editorStore).
3. `JobList.tsx`: when `cvStructureExists === false`, render banner with CTA opening the CV editor (`setEditorOpen(true)`).
4. i18n keys via `useT()` (per CLAUDE.md convention): `cvGate.title`, `cvGate.body`, `cvGate.cta` in `strings.en.json`; run `scripts/translate-ui.sh` (incremental).
5. Rebuild served bundle: `cd frontend && npm run build` (jsa serves gitignored `jsa/static`, not live source).

## Phase 6 — Tests (fakes over mocks; FakeAgentBackend; TDD)

**Baseline first:** 27 backend + 9 frontend tests already fail on HEAD (pre-existing debt) — record `pytest -v` baseline; don't count those as breakage.

Update:
- `tests/backend/test_stages.py` `TestCvAdjustConsumesBaseStructure` (~:953): assert `"CV TEXT:" not in msg` when structure present; replace `test_falls_back_to_cv_text_when_file_absent` with a test asserting neither skeleton nor raw CV appears when absent.
- `tests/backend/test_research.py` (~:241, :262, :284): rewrite message-shape assertions against `BASE CV STRUCTURE` / `JOB DESCRIPTION` / `TIER`.
- `tests/backend/test_fit_assessment.py`: add prompt-content tests via capturing FakeAgentBackend (reuse `_CapturingBackend` pattern, test_stages.py:939) — fit message contains markdown-rendered structure, not `job.cv_text`.
- `tests/backend/test_scaffold.py`: invoking without `--cv` no longer errors for a missing option.

New (`tests/backend/test_cv_source_of_truth.py`):
- Gate: orchestrator + missing structure file + pending job → stays `pending`, never dispatched; create file + `kick()` → dispatched.
- Kick-on-save: PUT `/api/cv-structure` calls `app.state.orchestrator.kick()` (recording stub).
- `/api/config` reports `cv_structure_exists` false → true after save.
- CLI bootstrap via `_bootstrap_cv_structure` + FakeAgentBackend: (a) seeds and saves valid structure; (b) existing structure → infer not invoked, note printed; (c) no `--cv`, no structure → proceeds, no `cv_text` stamped; (d) `InferError` → exit 1.

Frontend (vitest): JobList banner shown/hidden/CTA; store hydration of `cvStructureExists`; editorStore commit flips it true.

## Phase 7 — Docs + graph

- `README.md`: `--cv` now optional ("bootstrap only: seeds the CV structure once"); add a no-`--cv` example; extend base-CV section — single source of truth, jobs pend until it exists.
- `CLAUDE.md` "How to test a phase": smoke commands still valid (flag optional); note bad-extension error applies only when `--cv` is provided; add a no-`--cv` smoke line. (No ARCH.md file exists in the repo — nothing to update.)
- `graphify update .` after code changes.

## Verification

```bash
pytest -v -m "not integration"     # compare against HEAD baseline
cd frontend && npm test && npm run build
# Smoke:
jsa --csv jobs.csv --cv resume.pdf --no-browser   # 1st run: infer progress, writes cv_structure.json
jsa --csv jobs.csv --cv resume.pdf --no-browser   # 2nd run: "--cv is ignored" note
rm ~/.jsa/cv_structure.json && jsa --csv jobs.csv --no-browser  # jobs pend, banner shows; save structure in editor → job runs without restart
curl http://localhost:8765/api/config              # cv_structure_exists reflected
```

## Risks

- **Infer-at-startup latency/cost**: one LLM call before the server starts; backend auth/network failure blocks startup → clear error + exit 1; user can start without `--cv` and use the editor.
- **Mid-flight sessions**: jobs parked `awaiting_input` resume by replaying persisted history containing the old `CV TEXT` block — harmless (resume never rebuilds the initial message); re-run/reset jobs get the new prompt.
- **User-edited prompt stubs**: preserve sentinel sections exactly.
- **Gate inert when `cv_structure_path=None`**: intentional (test/legacy callers); production always passes it — document in docstring.

## Git

Branch from `main`: `feat/cv-structure-source-of-truth`. No merge without explicit approval.
