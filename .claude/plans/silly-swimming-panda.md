---
status: InProgress
---

# Phase 2 — CV Structure Editor (standalone CVDocument JSON, consumed by jobs)

> Phase 1 ("Structured-output CV/cover-letter pipeline") is **Done** (git `99f6a35`). This
> file is repurposed for the Phase 2 editor.

## Context

Phase 1 made `cv_adjust` emit a validated `CVDocument` JSON, deterministically serialized to
Markdown → PDF/DOCX. Phase 2 introduces a **standalone, always-available editor** for a single
canonical `CVDocument` JSON — the user's structured **base CV** — that lives independently of
any job at **`~/.jsa/cv_structure.json`** (next to `jsa.sqlite`). The user reshapes it freely
anytime (add/remove/rename/reorder sections & entries, edit every field). It is **not** tied to
a job: jobs are downstream **consumers** — `cv_adjust` reads this file as the authoritative
skeleton the per-job tailored CV conforms to. Design spec:
`.claude/designs/design_handoff_cv_structure_editor/` (README + `CV Structure Editor.dc.html`
prototype — **recreate as React+TS+Tailwind, do not copy the HTML**).

User scope decisions (this session):
- Editor is **standalone**, freely available anytime — **not** launched from / bound to a job.
- Canonical JSON = **a single file** `~/.jsa/cv_structure.json` (one active base CV; multiple
  named structures = backlog).
- **All three views** (Blocks primary, Document WYSIWYG paper, Split outline+paper).
- **Build infer/upload now** (upload a CV → derive structure → 5-step progress → populate).
- **Also wire job consumption now**: `cv_adjust` consumes the base structure as the skeleton.
- **Elevated warm palette** (cream/indigo, Geist+Newsreader) for the editor, **plus a backlog
  item to migrate the whole app to this palette later.**

### Key codebase findings that shape the design
- The base CV today is **raw text**: `load_cv` (`jsa/ingest/cv_loader.py:6`) → `job.cv_text`
  at CLI preflight (`jsa/cli.py:191,199`), fed verbatim into the `cv_adjust`/`cover_letter`
  prompts (`jsa/pipeline/stages.py:593,827`). There is **no structured base CV** yet — this
  phase creates it.
- Data dir is `~/.jsa/` (`Settings.db_path`, `jsa/config.py:16`). `CVDocument.model_validate`
  + `cv_to_markdown` (`jsa/schema/cv.py`, `jsa/render/serialize.py`) already exist and own
  validation + layout — the editor and store reuse them; **do not** add `kind`/`type` to the
  schema (those are UI-only).
- The app has **no router**; it is Zustand-driven (`store.ts`, App-level `JobList`+`JobDetail`
  split in `App.tsx`). The full-screen editor is toggled by **store state**, reached from a
  top-level **Header** entry — available regardless of selected job.
- WS is a **single global socket** (`ws.ts`, `store.ts::connectWS`); events are dispatched in
  `applyEvent`. Infer progress is a new global event keyed by a transient `task_id` (no job).

## Approach

```
Header [Structure Editor] ─► full-screen Editor (store: editorOpen)
   GET /api/cv-structure
     ├─ exists ─► load → edit (Blocks / Document / Split), live JSON drawer
     └─ 404   ─► empty state → Infer from CV (upload) | Start blank
                   POST /api/cv-structure/infer (multipart)
                   WS infer_progress ×5  ─►  CVDocument JSON (in-memory)
   Done ─► PUT /api/cv-structure ─► validate → write ~/.jsa/cv_structure.json

Jobs (consumers):  cv_adjust stage reads ~/.jsa/cv_structure.json if present,
                   injects it as the authoritative skeleton into the prompt.
```

No job state changes and no job re-render are involved in the editor — it is fully decoupled.

---

## Phase 2A — Standalone store + API (`jsa/`)

1. **Store module** `jsa/store/cv_structure.py` (new): `load() -> CVDocument | None` (read the
   file → `CVDocument.model_validate`; `None` if absent), `save(cv: CVDocument)` (validate →
   write pretty JSON), `path(settings)` = `settings.db_path.parent / "cv_structure.json"`.
   Wrap file IO in `await asyncio.to_thread(...)`. Add `cv_structure_path` to `Settings`
   (default derived from `db_path.parent`) so it is overridable/testable.
2. **Endpoints** (new router `jsa/api/routes_cv_structure.py`, mounted in `jsa/server.py`):
   - `GET /api/cv-structure` → stored JSON, or **404** if the file doesn't exist (drives the
     editor's empty state).
   - `PUT /api/cv-structure`, body `{structured: <CVDocument JSON>}` → `CVDocument.model_validate`
     (→ **422** with `exc.errors()[0]["msg"]` on the Phase 1 hard gates: `contact.name`, ≥1
     renderable section, `_not_a_cover_letter`) → `save()` → return the stored JSON.
   - `POST /api/cv-structure/infer`, multipart CV file upload → runs the one-shot inference
     **synchronously** and returns `{task_id, structured: <CVDocument JSON>}`. While running it
     broadcasts **5 progress steps** over the global WS as `{type:"infer_progress", task_id,
     step, total, label, status}` (the structured result is **not** carried in the event — only
     `status:"active"|"done"|"error"`). Steps map to real work: `Reading document` (`load_cv`) →
     `Detecting section breaks` / `Extracting entries & dates` / `Structuring JSON` (one one-shot
     LLM call) → `Validating against schema` (`model_validate`). The result is **not persisted**
     (the editor saves on Done). 422 on unparseable/invalid model output.
     **Frontend contract for Phase 2C:** fire the POST without blocking the UI, animate the
     checklist from the `infer_progress` WS events, and take the structured result from the POST
     **response body** (not from a WS event).
     *Known edges (deferred):* `GET` re-raises on a hand-corrupted file → 500 (only manual
     tampering; `save()` always writes validated JSON); `run_infer` maps `ProtocolError`→422 but
     not `AgentTimeout`/`AgentLimitReached` (→500).
   - One-shot LLM call: model after the `fit_assessment` single-turn invocation
     (`jsa/pipeline/stages.py`) — backend via `jsa/agents/registry.py`, parse the
     `<<<FINAL>>>…<<<END>>>` payload via `jsa/agents/protocol.py`, `json.loads` → validate.
3. **Infer prompt** `jsa/prompts/PROMPT_INFER_STRUCTURE.md` (new): "mirror this CV into a
   `CVDocument` JSON — no tailoring, no prose, JSON-only inside FINAL", with a compact schema +
   worked example. **Must end with the sentinel grammar block verbatim** (CLAUDE.md mandate).
4. **`infer_progress` event** in `jsa/events/schema.py` (+ WS publish on the existing bus).

## Phase 2B — Job consumption (`jsa/pipeline/stages.py`, `jsa/prompts/PROMPT_CDADJUST.md`)

- In the `cv_adjust` stage, **read the current base structure** via `jsa/store/cv_structure.py`
  at stage time (so edits apply to the next job with no restart). If present, inject its
  serialized/JSON form into the prompt as the **authoritative skeleton**: "Tailor content to the
  JD but **preserve this section structure** (same sections, same order, same shape)." Falls back
  to today's raw `cv_text` behavior when the file is absent (backward compatible).
- **Conformance is a soft skeleton, not a new hard gate.** Consistent with Phase 1's philosophy
  (a false reject kills a job), the base structure steers the prompt; the existing
  `CVDocument` schema validation stays the only hard gate. (A stricter "sections must match the
  base" check, if wanted, belongs in the self-heal nudge — like the summary nudge — not as a
  validation failure. Flagged as a tunable, defaulting to soft.)
- Update `PROMPT_CDADJUST.md` (keep its sentinel block verbatim) to document the injected
  skeleton and the "preserve structure" instruction.

## Phase 2C — Frontend foundation (`frontend/src/`)

- **Types** (`types.ts`): add `CVDocument`/`Section`/`Entry` (README §"Data Model"); add
  `{type:"infer_progress", task_id, step, total, label, status}` to `WSEvent`. Editor-local
  `EditorSection`/`EditorEntry` carry transient `id` + `kind`.
- **API** (`api.ts`): `getCvStructure()` (GET, treat 404 as "none"), `saveCvStructure(cv)` (PUT),
  `inferCvStructure(file)` (multipart POST → `{task_id}`).
- **Editor store** `frontend/src/editorStore.ts` (new, separate Zustand store): `cv`, `view`,
  `jsonOpen`, `inferring`, `inferStep`, `selectedId`, drag transients, `addOpen`, history
  `{history, hpos}`. Actions: field edits (**coalesced** ~600ms history push), structural ops
  (**immediate** push), `undo`/`redo` (flush pending coalesced first; cap ~120), view switch,
  infer flow (advanced by `infer_progress` events), `load`/`startBlank`, `exportJson()`.
  - `exportJson()` strips `id`/`kind`, omits empty strings/arrays, drops fully-empty entries
    (README §"Export rule").
  - **Kind inference on load** mirrors `serialize.py` name+shape classification (Summary/Profile
    → `summary`; Skills/Tech → `skills`; entries w/ bullets → `experience`; …) so `kind`
    round-trips without drift.
- **Entry point**: add `editorOpen: boolean` to `store.ts`; a **Header** button toggles it;
  `App.tsx` renders `<CvEditor/>` full-screen when open (else current split). Done →
  `saveCvStructure` → close.
- **Palette + fonts**: extend `tailwind.config` with the elevated tokens (accent `#3B5BD9`,
  canvas `#F4F2ED`, surface/subtle/sunk/border tiers, danger, JSON-highlight colors — README
  §"Design Tokens"); import Geist/Geist Mono/Newsreader + `cvspin`/`cvpulse`/`cvfade`/`cvbar`
  keyframes in `index.css`. Auto-resize textareas via `scrollHeight` (not `field-sizing`).

## Phase 2D — Views & components (`frontend/src/components/cv-editor/`)

- **Shell**: top bar (product label + candidate name + "N sections" pill; right cluster: view
  switcher, undo/redo, View/Hide JSON, Infer/Re-infer, **Done**).
- **View A — Blocks** (primary): `ContactCard`, `SectionCard` (drag grip, kind chip, name input,
  move/delete tools), body dispatch on kind (`summary` textarea / `bullets` list / `skills`
  tag-editor groups / `experience|projects|education` `EntryEditor` list), `AddSectionMenu`
  popover (6 kinds), inline `+` between cards. Drag armed only from the grip; plus up/down arrows.
- **View B — Document**: editable Newsreader "paper sheet"; fields sized to content; hover tools
  rail kept **inside** the sheet (no horizontal scroll).
- **View C — Split**: 280px outline rail (select/reorder) + the View-B paper in selectable mode
  with an accent focus ring on the selected section.
- **JSON drawer**: right panel, read-only syntax-highlighted `JSON.stringify(exportJson(),null,2)`,
  live, copy/close.
- **Empty state** (GET 404): Infer from CV / Start blank (single empty Summary section).
  **Inferring state**: 5-step checklist + progress bar driven by `infer_progress` WS events.
- **Client-side pre-validation** before Done (non-empty `contact.name` + ≥1 section with content)
  so Done rarely surprise-fails; still **surface the server 422** (e.g. letter guard) inline.
- Icons: reuse the codebase's inline-SVG style (~1.55 weight); no raster.

## Phase 2E — Tests
- **pytest** (`tests/backend/`): store load/save **round-trip** + 404 when absent; PUT **422 on
  the letter guard** and valid save writes the file; infer happy path with `FakeAgentBackend`
  (`tests/backend/fakes/fake_backend.py`); `cv_adjust` **consumption** — when the base file
  exists the injected skeleton appears in the prompt the fake backend receives (and absence
  falls back to `cv_text`). **Fakes, not mocks** (CLAUDE.md).
- **vitest** (`frontend/src/__tests__/`): editor-store **reorder**, **undo/redo coalescing**,
  `exportJson` **filtering**, **kind round-trip**. Must pass **independent of** the 9 known-red
  `ReviewPane.test.tsx` failures (memory `project-preexisting-test-debt`).

## Critical files
- **New (backend)**: `jsa/store/cv_structure.py`, `jsa/api/routes_cv_structure.py`,
  `jsa/prompts/PROMPT_INFER_STRUCTURE.md`.
- **Modified (backend)**: `jsa/config.py` (`cv_structure_path`), `jsa/server.py` (mount router),
  `jsa/events/schema.py` (`infer_progress`), `jsa/pipeline/stages.py` (cv_adjust consumption),
  `jsa/prompts/PROMPT_CDADJUST.md`, `tests/backend/`.
- **New (frontend)**: `frontend/src/editorStore.ts`, `frontend/src/components/cv-editor/*`,
  editor-store tests.
- **Modified (frontend)**: `types.ts`, `api.ts`, `store.ts` (`editorOpen`), `ws.ts`
  (`infer_progress`), `App.tsx`, `components/Header.tsx`, `tailwind.config.*`, `index.css`.
- **Untouched**: `jsa/schema/*` (editor conforms to the existing schema), `jsa/render/*`,
  `jsa/agents/protocol.py`, the DB models/pipeline state machine (no job rows, no migration).

## Verification
1. `pip install -e .` then `pytest -v -m "not integration"` — new backend tests pass; preexisting
   failure set unchanged.
2. `cd frontend && npm install && npm test` — new editor-store tests pass; the 9 known-red
   `ReviewPane` tests unchanged.
3. **Manual** (real backend — confirm before running): Header → Structure Editor on a fresh
   machine → empty state → Infer from CV (upload a PDF) → watch the 5-step progress → editor
   populates → reorder/rename/edit → **View JSON** updates live → **Done** writes
   `~/.jsa/cv_structure.json`. Re-open: it loads. A prose-only "Summary" that trips the letter
   guard surfaces a 422 inline. Then run a job (`jsa --cv … --csv …`) and confirm `cv_adjust`'s
   output mirrors the saved base structure's sections.

## Backlog / Phase 3 (deferred)
- **App-wide palette migration**: roll the elevated warm palette across Header/JobList/JobDetail/
  ReviewPane (user-requested follow-up).
- **Multiple named base structures** (the single-file store generalizes to a keyed store/table).
- **Hard conformance gate**: optionally enforce per-job sections match the base skeleton via a
  self-heal nudge (currently soft).
- **docx-js renderer** (Phase 1 carry-over) + the `*italic*`-in-DOCX `_add_runs` bug.

## Branch
New branch off `main`: `feat/cv-structure-editor`. No merge without explicit approval.
