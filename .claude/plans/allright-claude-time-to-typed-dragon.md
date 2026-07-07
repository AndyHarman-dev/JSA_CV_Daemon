---
status: Done
---

# Language Preference — global output + UI language

## Context

JSA is a single-user, local-only job-application tool. Today the LLM pipeline always
produces English deliverables (CV JSON, cover-letter JSON, clarifying questions,
change-logs, fit-assessment reasons) and the frontend chrome is hardcoded English. We want
one **global language preference** the operator sets once (a globe pill in the CV Structure
Editor top bar, per `.claude/designs/language_preference_design.zip`) that then governs
**three** things:

1. **Output data** — the CV and cover letter the model writes.
2. **The whole application UI** — every component's chrome, relabeled to the chosen language.
3. **The model's conversational output** — clarifying questions, change-logs, fit reasons.

(1) and (3) are one backend mechanism: a language directive injected into new-session system
prompts. (2) is a separate frontend i18n mechanism. This plan covers both, plus the
follow-on work the design handoff flags as mandatory (the English-only cover-letter guard
regression, and a maintainable translation pipeline).

Design handoff (already read): `.claude/designs/language_preference_design.zip` →
`README.md` is effectively the spec; `CV Structure Editor.dc.html` is the visual-fidelity
reference for the pill/panel.

## Decisions locked with the user

- **UI scope: the whole frontend** (~15 components), not just the CV editor. Because this is
  large, the mechanical string-extraction-and-translation work is spun into its own plan file
  executed by a **parallel subagent** (Workstream F).
- **Translation source: LLM-generated catalogs**, driven by a checked-in `.sh` runner, plus a
  persisted convention so future sessions re-translate when they add UI strings (Workstream G/H).
- **Cover-letter guard: fix now** (not ship-with-caveat). Because it is non-trivial, it is spun
  into its own plan file executed by a **parallel subagent** (Workstream C).

## Plan-mode note on the sub-plan files

Plan mode only permits editing this one plan file, so the two spun-off plans (Workstream C
and Workstream F) are fully specified inline below. **At execution start**, before any code,
create `.claude/plans/lang-pref-cl-guard.md` (from §C) and
`.claude/plans/lang-pref-frontend-i18n.md` (from §F), then launch each as a parallel subagent
once its dependencies (below) are in place.

---

## Architecture at a glance

```
Single source of truth for the language list:  jsa/i18n/languages.py
   ├── served to frontend via GET /api/config  → dropdown options + PUT validation
   ├── read by the pipeline directive           → code → human name
   └── read by the translation pipeline (.sh)    → which locales to generate

Global preference value:  ~/.jsa/preferences.json  ({"language":"en"})
   ├── jsa/store/preferences.py   (mirrors jsa/store/cv_structure.py)
   ├── GET/PUT /api/preferences   (mirrors routes_cv_structure.py)
   └── read fresh at stage time   (mirrors cv_structure.read(path))

Backend directive (output + conversation language):
   run_stage(..., preferences_path) → append directive only at start_session sites

Frontend:
   store.language + setLanguage()  → optimistic PUT
   Language pill/panel in CvEditor top bar
   i18n:  strings.en.json (source) + strings.<code>.json (generated) + useT() hook
```

## Dependency / ordering graph

Foundational (do first, in this order-ish; A/E are independent):
- **A** Backend preferences store + routes + config  (needs G's `languages.py` for validation)
- **G** Language catalog `jsa/i18n/languages.py` + translation pipeline `.sh`
- **E** Frontend i18n plumbing: `strings.en.json`, `useT()` hook, catalog index
- **D** Frontend language pill control + store wiring + hydration

Then, in parallel (each its own plan file + subagent):
- **C** Cover-letter guard fix — depends on **G** (`language_name`) only.
- **F** Whole-frontend translation — depends on **E** (hook + en catalog shape) and **G**
  (`languages.py` + `.sh` runner). Must start only after E is merged.

- **B** Pipeline directive injection — depends on **A** + **G**.
- **H** Persisted convention for future sessions — last, documents the finished pipeline.

---

## Workstream A — Backend preferences store, routes, config

Mirror the base-CV-structure pattern exactly (confirmed in
`jsa/store/cv_structure.py`, `jsa/api/routes_cv_structure.py`, `jsa/config.py`).

1. **`jsa/config.py`** — add a property next to `cv_structure_path` (`jsa/config.py:25-31`):
   ```python
   @property
   def preferences_path(self) -> Path:
       return self.db_path.parent / "preferences.json"
   ```
   Derives from `db_path` so tests that point `db_path` at a tmp dir get isolation for free.

2. **`jsa/store/preferences.py`** (new) — mirror `cv_structure.py`, but the key difference:
   **there is always a valid default**, so missing-file returns the default, never `None`.
   - `Preferences` pydantic model: `language: str = "en"`.
   - `_load_sync(path) -> Preferences`: if `not path.exists()` → `Preferences()`; else validate JSON.
   - `_save_sync(path, prefs)`: `mkdir(parents=True, exist_ok=True)` + `write_text(model_dump_json)`.
   - `async read(path) -> Preferences` (path-based, for stage-time reads — the analogue of
     `cv_structure.read`), `async load(settings)`, `async save(settings, prefs)`.
   - All IO in `asyncio.to_thread` (CLAUDE.md → Concurrency).

3. **`jsa/api/routes_preferences.py`** (new) — mirror `routes_cv_structure.py`:
   - `router = APIRouter()`; `_settings(request)` helper; `PreferencesBody(BaseModel){ language: str }`.
   - `GET /api/preferences` → `{"language": prefs.language}` (**no 404** — defaults always valid).
   - `PUT /api/preferences` → validate `body.language` against `jsa.i18n.languages.is_valid(code)`;
     `422` on unknown code; `save`; return the stored `{"language": ...}`.

4. **`jsa/server.py`** — register the router alongside the others
   (import near `jsa/server.py:18-21`, `app.include_router(preferences_router)` in the
   `:128-131` block, before `ws_router`).

5. **`jsa/api/routes_meta.py`** — add a `"languages"` key to the `/api/config` dict
   (`jsa/api/routes_meta.py:16-22`): `"languages": jsa.i18n.languages.LANGUAGES` (list of
   `[code, english, native]`). This is the single feed for the dropdown options.

**Tests** (`tests/backend/test_preferences.py`, mirror `test_cv_structure.py`): default when
absent, round-trip save/load, `read(path)` fresh-read, PUT 422 on bad code, GET returns default,
`/api/config` includes `languages`.

## Workstream G — Language catalog + translation pipeline

1. **`jsa/i18n/languages.py`** (new — the single source of truth, do NOT fork the list into TS):
   - `LANGUAGES: list[tuple[str, str, str]]` = ~20 curated `(code, english_name, native_name)`
     rows (lift the prototype's `LANGS` array from `CV Structure Editor.dc.html`).
   - `CODES: frozenset[str]`, `is_valid(code) -> bool`, `language_name(code) -> str`
     (english name; falls back to the code itself if unknown).
   - `__init__.py` for the `jsa/i18n` package.

2. **Translation generator** — `jsa/i18n/translate.py` (new): a build-time script (NOT imported
   by the server) that:
   - reads the source catalog `frontend/src/i18n/strings.en.json`,
   - reads target locales from `jsa.i18n.languages.LANGUAGES`,
   - for each locale, loads the existing `strings.<code>.json` (if any) and a sidecar
     `strings.meta.json` mapping `key → sha256(english_value_used)`,
   - translates **only** keys that are missing OR whose English source hash changed
     (idempotent, incremental — "updates translations as the app evolves"),
   - calls the Anthropic API (reuse the repo's existing anthropic dependency / `Settings`),
     one request per locale with a strict "translate values only, keep placeholders like
     `{n}` and keys verbatim, return JSON" instruction,
   - writes `strings.<code>.json` (sorted keys) + updates `strings.meta.json`.
   - Flags: `--force` (retranslate all), `--only <code>` (single locale), `--check`
     (exit non-zero if any locale is missing keys — for CI/pre-commit).

3. **`scripts/translate-ui.sh`** (new, `chmod +x`): thin wrapper —
   loads env (API key), `cd` repo root, `python -m jsa.i18n.translate "$@"`. This is the
   "ready-to-use `.sh` binary that runs the translation pipeline" the user asked for.

4. Do **not** hand-author locale JSONs; `strings.en.json` (Workstream E) is the only
   hand-maintained catalog. Everything else is generated by the runner.

## Workstream E — Frontend i18n plumbing

1. **`frontend/src/i18n/strings.en.json`** (new) — canonical source, flat dotted keys grouped by
   surface, e.g. `"cvEditor.commit": "COMMIT"`, `"cvEditor.rerun": "RE-RUN"`,
   `"cvEditor.runInference": "RUN INFERENCE"`, `"cvEditor.emptyTitle": "NO STRUCTURE DETECTED"`,
   `"lang.panelHeader": "LANGUAGE · OUTPUT + UI"`, `"lang.searchPlaceholder": "Search languages…"`,
   `"lang.footer": "Applies to LLM output and app labels everywhere."`, `"lang.noMatches": "No matches."`.
   Seed it with (a) the language-panel strings and (b) the CvEditor top-bar + empty-state
   strings (Workstream D uses these immediately); Workstream F extends it to the whole app.

2. **`frontend/src/i18n/index.ts`** (new) — import every `strings.*.json` via
   Vite's glob (`import.meta.glob("./strings.*.json", { eager: true })`) into a
   `Record<code, Catalog>`; export `getCatalog(code): Catalog`. New generated locale files are
   picked up automatically — no manual registration.

3. **`frontend/src/i18n/useT.ts`** (new) — `useT()` reads `language` from `useStore`, returns
   `t(key, vars?) => string` that looks up `getCatalog(language)[key]`, **falls back to the
   `en` catalog**, then to the key itself; supports `{n}`-style placeholder interpolation. This
   is the one hook every component uses so a language change re-renders everywhere reactively
   (same cross-cutting-signal shape as `lastBackendSwitch`).

## Workstream D — Frontend language pill control + store wiring

Fidelity reference: `CV Structure Editor.dc.html` (`renderLangPill()`), design tokens in
`README.md` → "Design Tokens". Use `EDITOR_THEME` (`frontend/src/theme/tokens.ts:108`) —
accent `T.a` `#FF4655`, `T.surface`, `T.ink3`, `T.btnRadius` (7, hud skin), `T.shadowMd`.

1. **`frontend/src/store.ts`** — add to the `Store` interface (near
   `store.ts:12-27`) and impl:
   - `language: string;` init `"en"`.
   - `hydrateLanguage(): Promise<void>` — `GET /api/preferences` → `set({language})`.
   - `setLanguage(code): Promise<void>` — optimistic `set({language: code})`, then
     `await api.putPreferences(code)`, revert on failure (model on `refetchAll`,
     `store.ts:105-118`, the existing api-calling async action).

2. **`frontend/src/api.ts`** — add `getPreferences(): Promise<{language:string}>` (thin
   `apiFetch` GET like `config()`, `api.ts:137-139`) and
   `putPreferences(language)` (PUT-with-JSON-body like `saveCvStructure`, `api.ts:113-120`).
   Also type the `config()` return to include `languages: [string,string,string][]`.

3. **`frontend/src/App.tsx`** — in the boot `useEffect` (`App.tsx:19-24`, next to
   `refetchAll()`), call `hydrateLanguage()` and fetch `api.config()` once to populate the
   language list (store it, e.g. `languages` field, or read from a small context). The pill's
   options come from `/api/config.languages`.

4. **`frontend/src/theme/Icon.tsx`** — add `"globe"` and `"search"` to the `IconName` union
   (`Icon.tsx:6-10`) and glyphs via `extraShapes()` (`Icon.tsx:58-108`): globe = outer
   `<circle r=6.2>` + horizontal diameter `<line>` + two vertical meridian arcs (paths);
   search = `<circle r≈4>` + short diagonal handle `<line>`. Match `strokeWidth 1.55`, round caps.

5. **`frontend/src/components/cv-editor/LanguagePill.tsx`** (new) — the pill + panel.
   - Closed pill: `cvghost`-styled, globe glyph (`T.a`), current code uppercased (mono 11px),
     rotating chevron. Insert into the right-hand cluster in **`CvEditor.tsx` before line 503**
     (the RE-RUN/RUN INFERENCE `FileButton`), reading order `[SRC.JSON] [🌐 EN ▾] [RE-RUN] [COMMIT]`.
   - Panel: reuse `panelBase(T, {chamfer})` + `cornerMarks(T,...)` + `T.shadowMd` (already
     imported in `CvEditor.tsx:12`); width 260, `z-index 40`, `animation: cvfade`. Header label
     (`t("lang.panelHeader")`), search input (filters code/english/native, case-insensitive
     substring), scrollable list (`max-height:240`) of rows (code / english / native / checkmark
     on selected, selected row `color-mix(accent 14%, surface)`), footer note. Empty →
     `t("lang.noMatches")`.
   - **Click-outside-to-close** via a `ref` + `mousedown` effect — copy the exact pattern from
     `ReviewPane.tsx:42-55` (the download menu) / `Header.tsx:99-108`. (The prototype omits this;
     the handoff says add it in production.)
   - Selecting a row calls `setLanguage(code)` and closes — no Apply step.
   - Options come from the `/api/config.languages` list; selection value is the ISO code.

6. Wire the CvEditor top-bar + empty-state strings **through `useT()`** as the first consumer
   (parity with the handoff): sub-label (`CvEditor.tsx:409`), `OPERATOR`/`MODULES` (`:416,:424`),
   `SRC.JSON`/`HIDE SRC` (`:499`), `RE-RUN`/`RUN INFERENCE` (`:503`), `COMMIT` (`:508`),
   empty-state title/body/`INIT BLANK` (`EmptyState`, `:98-126`). (Full-app coverage = Workstream F.)

## Workstream B — Pipeline language directive (output + conversation language)

All in `jsa/pipeline/stages.py` unless noted. Thread `preferences_path` exactly like
`cv_structure_path` is threaded (`Orchestrator.__init__` `orchestrator.py:104,112`;
call site `orchestrator.py:271-275`; `run_stage` signature `stages.py:317-324`).

1. **Thread the path**: add `preferences_path: Path | None = None` to `Orchestrator.__init__`
   and `run_stage(...)`; pass it at the single `run_stage` call site. Wire it from the CLI/server
   startup as `settings.preferences_path` (same place `cv_structure_path` is wired).

2. **Resolve the language at stage time, fresh** (no cache), via
   `preferences.read(preferences_path)` → code → `jsa.i18n.languages.language_name(code)`.
   Mirrors the `cv_structure.read` read-at-stage-time rationale (`stages.py:434-435`).

3. **Build the directive** (a helper `_language_directive(code, name) -> str`) — per README
   §Pipeline-Integration.1:
   - "Write all natural-language, user-facing output in {name} ({code}) — change-log,
     clarifying questions, and every text VALUE in the final JSON. Do **not** translate JSON
     keys/field names (`heading`, `subheading`, `bullets`, `text`, `items`, `entries` …) — they
     are fixed schema fields."
   - **Carve-outs (critical — else the parser breaks silently):**
     - Sentinel blocks `<<<FINAL>>>` / `<<<NEED_INPUT>>>` / `<<<END>>>` stay ASCII verbatim
       (matched literally in `jsa/agents/protocol.py:12-18`).
     - For `fit_assessment` **only**, add: the verdict word `FIT`/`UNFIT` must stay English and
       be the first word; only the reason after it is in {name}. (`_parse_fit_verdict`
       `stages.py:634-666` matches the literal uppercased substring.)

4. **Inject only on NEW sessions, never on resume** (README §5 — a resumed session already
   committed to a language; re-injecting a changed directive contradicts replayed history).
   `system_prompt` is computed once and shared by both `restore_session` and `start_session`
   (`stages.py:335`), so **do not** append to that shared value. Instead append the directive at
   the three `start_session` sites only:
   - cv_adjust / cover_letter fresh branch — the `else` at `stages.py:426-443` (append to the
     `system_prompt` local passed to `start_session`, or fold into `initial_user_msg` via
     `_build_initial_user_msg`, which is already new-session-scoped).
   - `_run_fit_assessment` — its `backend.start_session(...)` (`stages.py:686`), with the
     fit-specific carve-out.

5. **infer_structure is a separate path — handle it too.** `_get_system_prompt` does not cover
   `infer_structure`; it runs via `POST /api/cv-structure/infer` → `jsa/pipeline/infer_structure.py`
   (not `run_stage`). Thread the language / resolve `settings.preferences_path` there and append
   the same directive at its `start_session`. **Verify first** whether structure inference should
   even be re-languaged (its output feeds cv_adjust, which re-languages anyway) — if the team
   decides English structure is fine, document that and skip; do not silently skip.

**Tests** (`tests/backend/test_stages.py` additions, `FakeAgentBackend`): assert the directive
text appears in the system prompt / initial user msg for each fresh stage; assert it is **absent**
on `restore_session` (resume + revision) paths; assert the fit carve-out text is present for
fit_assessment; assert default `en` yields the English directive.

## Workstream C — Cover-letter guard fix  → separate plan file + parallel subagent

Spin into `.claude/plans/lang-pref-cl-guard.md`. Problem: `_not_a_cover_letter`
(`jsa/schema/cv.py:355-372`) uses `_LETTER_FORMULA_RE` (`cv.py:94-111`, English idioms:
"dear hiring", "sincerely," …, threshold 2). Once CV output can be non-English, a mis-emitted
non-English cover letter sails through as the CV undetected.

- **Chosen fix: per-language formula lists** keyed by the same `jsa.i18n.languages` catalog —
  a `dict[code, tuple[pattern,...]]` of the ~6 letter idioms per supported language, selected by
  the job's active language (read via `preferences.read`, threaded into the CV schema validation
  or applied as a post-parse check in the cv_adjust stage rather than inside the pydantic
  validator, since the validator has no language context). Keep the English list as the default.
- Alternative if per-language authoring stalls: a to-English gloss step before the guard. Note
  in the sub-plan; per-language is the primary.
- Depends on Workstream G (`languages.py`). Tests: a Spanish/French/German cover-letter blob
  trips the guard; a legitimate non-English CV does not.

## Workstream F — Whole-frontend translation  → separate plan file + parallel subagent

Spin into `.claude/plans/lang-pref-frontend-i18n.md`. **Starts only after Workstream E merges**
(needs `useT()` + `strings.en.json` shape) and Workstream G (`languages.py` + `.sh` runner).

- Mechanical pass over ~15 components (`Header.tsx`, `JobDetail.tsx`, `ReviewPane.tsx`,
  `JobList`, editor subtrees, modals, empty states, the "not a fit" modal, scratch-buffer /
  mention-dropdown UIs, etc.): extract every hardcoded English string into
  `strings.en.json` with a stable dotted key, replace the literal with `t("key")` from `useT()`.
- Run `scripts/translate-ui.sh` to generate all locale catalogs from the extended `strings.en.json`.
- Verify no user-visible English literal remains (a lint/grep check over `frontend/src` for
  JSX text nodes and common label props), and that switching language re-renders all surfaces.
- This is the large, parallelizable chunk — hand to a subagent with the key-naming convention
  and the `useT()` contract from Workstream E.

## Workstream H — Persisted convention for future sessions

So future work keeps translations in sync (the user's "local written memory … verify new
elements are translated" ask). Do **both**:

1. **Project `CLAUDE.md`** — add a short "UI string i18n" convention section (this file is the
   home for non-derivable conventions for all agents): *any new user-visible frontend string must
   be added to `frontend/src/i18n/strings.en.json` with a dotted key and rendered via `useT()`;
   after adding strings, run `scripts/translate-ui.sh` (or `--check` in CI) to regenerate locale
   catalogs; never hardcode English in JSX.*
2. **Auto-memory** — write a `feedback`-type memory file under
   `.claude/projects/-Users-wiam-VSCodeProjects-JSA/memory/` (per the memory convention:
   frontmatter + **Why** / **How to apply**) capturing the same rule, and add its one-line pointer
   to `MEMORY.md`.

---

## Verification (end-to-end)

Backend:
- `pip install -e .` (new `jsa/i18n` package), then `pytest tests/backend/test_preferences.py
  tests/backend/test_stages.py -v` — new store, routes, config `languages`, directive
  present-on-new / absent-on-resume, fit carve-out, guard fix.
- Manual: `curl localhost:8765/api/config` shows `languages`; `curl -XPUT
  localhost:8765/api/preferences -d '{"language":"es"}'` returns `{"language":"es"}`;
  bad code → 422; `GET` before any PUT returns `{"language":"en"}`.

Pipeline smoke (integration, opt-in): set language `es`, run a job with a fake/real backend,
confirm the system prompt/initial user msg carries the Spanish directive on a fresh stage and
NOT on a resumed/revision stage; confirm `FIT`/`UNFIT` stays English.

Frontend:
- `cd frontend && npm install && npm test` (vitest) — LanguagePill open/close/search/select,
  click-outside, `useT` fallback to en, store optimistic set + revert on PUT failure.
- `npm run build` **(mandatory after any UI change — the jsa CLI serves the built
  `jsa/static` bundle, not live source)**, then `jsa --csv … --cv …`, open the CV editor:
  the globe pill sits before RE-RUN, opens a searchable 260px panel, selecting a language
  relabels the top bar + empty state immediately and persists across reload.
- Translation pipeline: `scripts/translate-ui.sh --check` passes; adding a new key to
  `strings.en.json` and re-running fills only that key across locales.

## Pre-existing test debt (do not be alarmed)

Per memory: 27 backend + 9 frontend tests already fail on HEAD (fit-gate + JSON-fixture debt),
unrelated to this work. Judge success by the new/adjacent tests, not the global suite.
