---
status: InProgress
---

# CV Decks — many base CVs + per-job base-CV assignment

## Context

Today JSA maintains **exactly one** base CV: `~/.jsa/cv_structure.json`, a single validated
`CVDocument` edited in the CV Structure Editor and read at stage time by `fit_assessment`
(as `cv_to_markdown`) and `cv_adjust` (as the `BASE CV STRUCTURE` JSON skeleton). One CV for
every job means a candidate applying across two different profiles (say, backend vs. data)
has to hand-edit the single structure before each launch, or accept a worse tailoring base.

The design hand-off (`~/Downloads/CV_Decks_Feature.zip` →
`design_handoff_cv_decks_feature/`) adds two things:

1. **CV Structure Editor** — a hover-out flyout rail on the left listing every base CV
   ("deck"), with switch / rename / duplicate / delete / new. Each deck is independently
   editable and persists.
2. **JSA App Shell** — every pre-launch job row gets a doc-icon trigger opening a popover to
   pick which base CV that job uses. The picked deck is *exactly* what gets injected as the
   base CV for that job's `fit_assessment` and `cv_adjust` prompts.

Outcome: the user picks a base CV per job, choosing the one already closest to the target
role instead of making one CV serve everything.

The hand-off's `.dc.html` files are design references in the design tool's own component
format — layout, copy, spacing and interaction logic are the spec; the code is not to be
pasted. The hand-off also renders the **prompt-injection** syringe button next to our new
doc button; that feature is a separate in-flight effort (see "Concurrent work" below) and is
**out of scope here**.

### Storage reality vs. the hand-off

The hand-off persists decks to `localStorage` under `jsa_cv_decks`. That is a design-tool
artifact and cannot be used: the **Python pipeline** must read the chosen CV at stage time,
so decks must live server-side. Everything else in the hand-off (layout, tokens, copy,
interaction rules) is honored.

---

## Locked decisions

1. **A deck is a file, and the pipeline receives its path — `stages.py` is not touched.**
   `run_stage(..., cv_structure_path=<path>)` already takes a path to a single JSON file
   holding one `CVDocument`. Each deck's file is *exactly that format*, so the orchestrator
   resolves `job.base_cv_id` → deck path and passes it as `cv_structure_path`. Zero changes
   to `jsa/pipeline/stages.py`, and `_read_base_structure` / `_base_structure_cv_block` /
   `_build_fit_user_msg` are reused verbatim. **Do not "simplify" this later into a
   `run_stage` signature change** — beyond being a smaller diff, `stages.py` is currently
   contended by two other worktrees (see "Concurrent work").
2. **No prompt-caching impact.** The base CV goes into the *user message*
   (`_build_fit_user_msg` / `_build_initial_user_msg`), never into the system prefix
   assembled by `prompt_assembly.assemble_system_prompt`. Per-job decks therefore do not
   violate CLAUDE.md's cross-job system-prefix invariant.
   `tests/backend/test_prompt_prefix_stability.py` stays untouched and green.
3. **`/api/cv-structure` GET/PUT stay, as aliases for the default deck.** Cheap, keeps
   `tests/backend/test_cv_structure.py` + `test_cv_source_of_truth.py` meaningful, and
   nothing has to be deleted. `POST /api/cv-structure/infer` is already deck-agnostic
   (returns an unsaved structure) and is **unchanged**.
4. **The legacy `cv_structure.json` is migrated by copy and then left alone, forever.**
   Never deleted, never rewritten. It becomes an inert backup of the pre-decks state.
5. **The deck index denormalizes the auto-title.** `GET /api/cv-decks` must not fan out into
   one file read per deck (it feeds the job-row picker). The index caches
   `auto_title = cv.contact.name`, recomputed on every deck save; the deck file stays the
   source of truth.
6. **Deck ids are server-generated `uuid4().hex`** and are used as filenames. Client-supplied
   ids are rejected on create; every path derivation re-validates `^[0-9a-f]{32}$` so a
   `../` id can never escape `~/.jsa/cv_decks/`.
7. **Assignment is pre-launch only** — `PUT /api/jobs/{id}/base-cv` returns 409 unless
   `job.state == queued` (our `queued` is the hand-off's "pending": fresh ingest, parked
   until LAUNCH). Matches the hand-off's "pre-launch only" restriction.
8. **The DTO carries `name` + `auto_title`, not a rendered `label`.** The
   "Untitled CV" fallback is a UI string and must go through `useT()`; a server-rendered
   label would hardcode English into the API.

### Decisions taken with the user (2026-09-04)

- **Unsaved invalid drafts on switch → discard, with a confirm.** Switching flushes and, if
  dirty, attempts a save; a valid CV is persisted (nothing lost). If it 422s (a blank deck
  can't validate — `CVDocument` needs a non-empty `contact.name` *and* ≥1 renderable
  section), a `confirm()` warns the draft will be lost and the switch proceeds on OK.
  Rationale: matches today's behavior — closing the editor without COMMIT already discards.
- **Explicit default deck.** The index carries `default_id`; the rail gets a 4th row icon
  (star) to set it. An unassigned job uses the default deck. The picker popover tags that
  deck `DEFAULT` so an unassigned job's fate is legible.
- **Deleting a deck clears the assignment on undispatched jobs only** —
  `base_cv_id = NULL` where `state IN (queued, pending)`, via `jsa/db/repo.py`, never raw
  SQL from a route. Dispatched/finished jobs keep their value as a record of what was
  actually submitted.

---

## Concurrent work (collision map)

Two other worktrees are mid-flight off the same `main` (`e9c278a`). Nothing here blocks on
them, but three files are shared surfaces:

| Surface | Who else | Note |
|---|---|---|
| `jsa/pipeline/stages.py`, `prompt_assembly.py` | `feat/prompt-injection` Phase 2 (not started), `feat/revision-tool-use` (in progress) | **We touch neither.** This is decision 1's payoff. |
| `jsa/db/models.py` + `engine.py::init_db` | `feat/prompt-injection` adds `Job.injection` | Both are additive columns in their own `try/except OperationalError` block. Mechanical merge. |
| `frontend/src/components/JobList.tsx` queued branch (~line 114) | `feat/prompt-injection` Phase 4 adds the syringe trigger | Build the footer as a flex row `<BaseCvTrigger/> … <LaunchButton/>`; per the hand-off's own ordering (`cvTriggerBtn, injectTriggerBtn, launchSlot`) the syringe slots **between** them, so whoever lands second does a one-line insert. |
| `frontend/src/theme/Icon.tsx` | injection adds `syringe` | We add `pencil` + `star`. Additive to the same union + `PATHS` map. |
| `jsa/server.py` router registration | injection adds `routes_injection_presets` | Additive lines. |

---

## Phase 0 — Worktree setup

Branch **from local `main`** (`e9c278a`) so this shares a merge base with the other two
worktrees:

```
EnterWorktree → .claude/worktrees/cv-decks, branch feat/cv-decks
```

- **Do NOT run `pip install -e .` in the worktree** — it hijacks the global `jsa` entry
  point; the symptom is `{"detail":"Not Found"}` at `/`. Run the suite as
  `python -m pytest` from inside the worktree; smoke-test the live server from the main
  checkout after merge.
- Frontend work runs `npm install`/`npm test` inside the worktree's `frontend/` normally.

---

## Phase 1 — Deck store + config paths

**Model:** `sonnet` is sufficient (mechanical, pattern-following).

### Current state
`jsa/store/cv_structure.py` (59 lines) is a single-file store for one `CVDocument` at
`Settings.cv_structure_path` (a derived `@property`, `db_path.parent / "cv_structure.json"`).
`preferences.py` and `backend_models.py` mirror the same shape: pydantic model →
`_load_sync`/`_save_sync` → `read(path)` / `load(settings)` / `save(settings, model)`, all
FS IO wrapped in `asyncio.to_thread`.

### Desired state
A new store owning **many** decks, each a `CVDocument` in its own file, plus a small index
holding order, names, denormalized auto-titles and `default_id`.

### Problems
- Nothing in the codebase knows about more than one CV. `CVDocument` has no id/name field
  and must not gain one (it is the pipeline's wire schema and the editor's export format).
- The list endpoint feeds the job-row picker, so it must be one cheap read (decision 5).
- Deck ids become filenames → path-traversal surface (decision 6).

### Solutions

**`jsa/config.py`** — two new derived properties next to `cv_structure_path`, same
`db_path`-derived idiom so a `tmp_path` `db_path` auto-isolates tests:

```python
@property
def cv_decks_path(self) -> Path:        # the index
    return self.db_path.parent / "cv_decks.json"

@property
def cv_decks_dir(self) -> Path:         # one CVDocument per deck
    return self.db_path.parent / "cv_decks"
```

**`jsa/store/cv_structure.py`** — one additive change: extract a public path-based writer so
the deck store reuses it instead of duplicating `_save_sync`.

```python
async def write(path: Path, cv: CVDocument) -> None:
    await asyncio.to_thread(_save_sync, path, cv)

async def save(settings: Settings, cv: CVDocument) -> None:
    await write(structure_path(settings), cv)   # delegates; behavior unchanged
```

**`jsa/store/cv_decks.py`** (new) — mirrors the three-store pattern exactly:

```python
_DECK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

class DeckMeta(BaseModel):
    id: str
    name: str | None = None          # user label; None → fall back to auto_title
    auto_title: str | None = None    # cached cv.contact.name, refreshed on every save
    has_cv: bool = False             # False for a created-but-never-saved deck slot

class DeckIndex(BaseModel):
    decks: list[DeckMeta] = []
    default_id: str | None = None    # always set while decks is non-empty
```

Functions (all async, all IO via `asyncio.to_thread`):

- `index_path(settings)`, `deck_path(settings, deck_id)` — the latter validates
  `_DECK_ID_RE` and raises `ValueError` otherwise.
- `read_index(path) -> DeckIndex` / `load_index(settings)` / `save_index(settings, index)` —
  absent file → `DeckIndex()` (the `preferences.py` default-instance convention, *not*
  `cv_structure.py`'s `None`).
- `load_deck(settings, deck_id) -> CVDocument | None` — delegates to
  `cv_structure.read(deck_path(...))`.
- `save_deck(settings, deck_id, cv)` — `cv_structure.write(...)`, then refresh that deck's
  `auto_title` + `has_cv` in the index and re-save it (decision 5's cache write).
- `create_deck(settings, *, name=None) -> DeckMeta` — `uuid4().hex` id, appended to the
  index, no file written yet (`has_cv=False`). Becomes `default_id` if it is the first deck.
  **A `has_cv=False` slot is a real, reachable state** (the "NEW BASE CV" screen), so it must
  never be assignable to a job or resolvable by the pipeline — see Phases 2, 3 and 6.
- `rename_deck`, `set_default`, `duplicate_deck(settings, src_id, *, name)`,
  `delete_deck(settings, deck_id)` — delete removes the file (`missing_ok=True`), drops the
  index entry, and promotes `decks[0]` to default if the deleted deck was the default.
- `resolve_path(settings, deck_id: str | None) -> Path | None` — the pipeline seam. Returns
  the first **existing** file among: the requested deck → `default_id` → index order. Pure
  existence checks (cheap enough for per-dispatch use); loadability/corruption stays
  `stages._read_base_structure`'s job, exactly as today. Logs a warning when a requested
  deck id could not be honored.
- `migrate_legacy(settings)` — called from `load_index` when the index file does **not**
  exist: if `cv_structure_path` exists and parses, mint a deck id, **copy** the file into
  `cv_decks/<id>.json`, write an index with that deck as default, and leave the legacy file
  in place (decision 4). A corrupt legacy file is treated as absent (warning logged) — the
  gate then reports "no usable CV structure", the same user-visible outcome as today. When
  no legacy file exists, return an empty `DeckIndex()` **without** writing the file, so a
  fresh install has no state until the user creates something.

### Tests — `tests/backend/test_cv_decks.py` (new)
Round-trip create/save/load/rename/duplicate/delete; `auto_title` refresh on save;
`default_id` assignment on first deck and promotion on delete-the-default; `deck_path`
rejects `../x`, `""`, and non-hex ids; migration copies the legacy file, leaves it on disk,
and is a no-op the second time; empty install writes no index file.

---

## Phase 2 — Deck HTTP API + config signal

**Model:** `sonnet`.

### Current state
`jsa/api/routes_cv_structure.py`: `GET`/`PUT /api/cv-structure` (+ `orchestrator.kick()` on
PUT) and `POST /api/cv-structure/infer`. `jsa/api/routes_meta.py:20` computes
`cv_structure_exists` as `settings.cv_structure_path.exists()`.

### Desired state
Full deck CRUD, plus the legacy singular endpoints re-pointed at the default deck, plus a
deck-aware `cv_structure_exists`.

### Problems
- `cv_structure_exists` drives `JobList`'s gate banner. After migration the legacy file is
  inert, so an `exists()` check on it becomes a lie (true forever, even with zero decks).
- The editor's save must keep unblocking the orchestrator without a restart (`kick()`).

### Solutions

**`jsa/api/routes_cv_decks.py`** (new router, registered in `server.py` next to
`cv_structure_router`):

```
GET    /api/cv-decks                -> {"decks":[{id,name,auto_title,has_cv,is_default}],
                                        "default_id": str|null}
POST   /api/cv-decks                -> body {"name": str|null}  -> 201 {"deck": {...}}
GET    /api/cv-decks/{id}           -> {"structured": {...}}   | 404 if has_cv is False
PUT    /api/cv-decks/{id}           -> body {"structured": {...}}; validate (422 via the
                                        existing `_concise_reason`), persist, kick()
PATCH  /api/cv-decks/{id}           -> body {"name"?: str|null, "is_default"?: true}
POST   /api/cv-decks/{id}/duplicate -> body {"name": str|null} -> 201 {"deck": {...}}
DELETE /api/cv-decks/{id}           -> 204; clears base_cv_id on undispatched jobs (Phase 3)
```

`GET /api/cv-decks` returns **every** deck, empty slots included — the rail needs to show a
just-created slot. Filtering `has_cv` for the job-row picker is the *client's* job (Phase 6),
deliberately, so the two consumers don't need two endpoints.

Reuse from `routes_cv_structure.py`: the `CvStructureBody` shape, the
`ValidationError → 422 _concise_reason` idiom, and `request.app.state.orchestrator.kick()`
on every successful deck write. Unknown deck id → 404. `ValueError` from `deck_path` → 400.

**`routes_cv_structure.py`** — the two singular endpoints become thin default-deck aliases
(same paths, same response shapes, same 404/422/kick semantics):
- `GET` → `load_deck(settings, index.default_id)`; 404 when there is no default or it has no
  CV yet.
- `PUT` → `save_deck` into `default_id`, creating a deck first if the index is empty (this is
  what keeps a fresh-install `PUT /api/cv-structure` working, and what
  `test_cv_source_of_truth.py::TestConfigSignal` exercises).
- `POST /api/cv-structure/infer` — **unchanged** (stateless, returns an unsaved structure;
  the editor PUTs it into whichever deck is active).

**`routes_meta.py`** — `cv_structure_exists` becomes "at least one deck has a CV":
`bool(cv_decks.resolve_path(settings, None))`. Field name is kept (the frontend store reads
it). Add `cv_deck_count: int` alongside it for the picker's empty-state copy.

### Tests — `tests/backend/test_cv_decks_api.py` (new)
Full CRUD happy paths + 404/400/422; `kick()` fired on PUT/PATCH-default/DELETE;
`GET /api/cv-decks` is a single index read (assert the deck files are never opened — patch
`cv_structure.read` and count calls); legacy-alias equivalence (a `PUT /api/cv-structure`
followed by `GET /api/cv-decks` shows one deck, and `GET /api/cv-structure` returns it);
`cv_structure_exists` false → true → false across create/save/delete.

---

## Phase 3 — `Job.base_cv_id` + assignment endpoint

**Model:** `sonnet`.

### Current state
`Job` has no CV-selection column. `jsa/api/routes_jobs.py::_job_to_dict` builds the job DTO.
`jsa/db/engine.py::init_db` does `create_all` plus one additive
`ALTER TABLE … ADD COLUMN` per column, each in its own `try/except OperationalError: pass`.

### Desired state
A nullable per-job deck reference, writable only pre-launch, visible on the job DTO.

### Problems
- SQLite here has no column-drop path and no FK enforcement elsewhere in the schema, so this
  must be a plain nullable `String` and dangling ids must resolve gracefully (Phase 4).
- Assigning after launch would silently not take effect for stages already replayed → 409.

### Solutions

**`jsa/db/models.py`** — one column on `Job`:
```python
base_cv_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
# deck id from cv_decks.json; NULL = use the default deck. Set pre-launch only.
```

**`jsa/db/engine.py::init_db`** — its own block at the bottom, matching the file's style:
```python
try:
    await conn.execute(text("ALTER TABLE jobs ADD COLUMN base_cv_id VARCHAR(32)"))
except OperationalError:
    pass  # column already exists — safe to ignore
```

**`jsa/db/repo.py`** — two helpers (routes must not write raw SQL):
- `set_job_base_cv(session, job, deck_id: str | None)` — plain field write + commit.
- `clear_base_cv_assignments(session, deck_id) -> int` — `base_cv_id = NULL` where
  `base_cv_id == deck_id AND state IN (queued, pending)`; returns the count. Called by
  `DELETE /api/cv-decks/{id}`; deliberately scoped so `review`/`approved`/`failed` rows keep
  the record of which base CV they were built from.

**`jsa/api/routes_jobs.py`**
- `_job_to_dict`: add `"base_cv_id": job.base_cv_id`.
- `PUT /api/jobs/{job_id}/base-cv`, body `{"deck_id": str | null}`: 404 unknown job, 409
  unless `state == queued` (message: "base CV can only be assigned before launch"), 422 when
  `deck_id` is not `None` and either no such deck exists in the index **or that deck has
  `has_cv=False`** (an empty slot must not be assignable — otherwise `resolve_path` finds no
  file and silently falls back to the default, i.e. the user picks deck B and the model gets
  deck A with only a log line to show it). Emits nothing on the event
  bus — the frontend patches its own store optimistically, same as other job mutations.

### Tests — `tests/backend/test_job_base_cv.py` (new)
Assign / reassign / unassign on a `queued` job; 409 on `running`/`review`/`approved`; 422 on
an unknown deck id; DTO exposure; `clear_base_cv_assignments` clears `queued`+`pending` and
leaves `review`/`approved` untouched; the DELETE route wires it up.

---

## Phase 4 — Orchestrator resolution + gate (the crux)

**Model:** `opus` — this phase carries the parity gate and the fallback semantics.

### Current state
```python
# jsa/pipeline/orchestrator.py:288 — the gate, once per dispatch cycle, global
if self._cv_structure_path is not None and (
    await stages._read_base_structure(self._cv_structure_path)
) is None:
    ... publish "No usable CV structure ..." once; await self.wakeup.wait(); continue
```
```python
# jsa/pipeline/orchestrator.py:473 — inside _run_one, which already holds `job`
await stages.run_stage(job, backend, stage, session, ...,
                       cv_structure_path=self._cv_structure_path, ...)
```
`server.py:277` passes `cv_structure_path=settings.cv_structure_path`. The orchestrator
cannot import `Settings` (circular via `server.py`), which is why paths/factories are
injected — `make_model_resolver` is the established idiom.

### Desired state
The dispatch site resolves the **job's own** deck path; the gate asks "is any deck usable".
Existing callers that pass only `cv_structure_path` keep today's behavior byte-for-byte.

### Problems
- A per-job path cannot come from a constant field; it needs a resolver the orchestrator can
  call with `job.base_cv_id`.
- Two code paths (legacy path vs. resolver) inside `_run_one` and the gate would rot. Collapse
  them in `__init__` instead.
- A deleted or corrupt deck must never hard-fail a job.

### Solutions

**`jsa/server.py`** — a `make_base_cv_resolver(settings)` factory beside
`make_model_resolver`, returning `Callable[[str | None], Awaitable[Path | None]]` that just
calls `cv_decks.resolve_path(settings, deck_id)`. Passed to `Orchestrator(...)` as
`base_cv_resolver=`; `cv_structure_path=` is no longer passed in production.

**`jsa/pipeline/orchestrator.py`**
```python
def __init__(self, ..., cv_structure_path: Path | None = None,
             base_cv_resolver: BaseCvResolver | None = None, ...):
    # One internal code path: a bare cv_structure_path (tests, legacy callers) is
    # wrapped into a resolver that ignores the deck id — today's exact behavior.
    if base_cv_resolver is None and cv_structure_path is not None:
        async def base_cv_resolver(_deck_id, _p=cv_structure_path):  # noqa: ARG001
            return _p
    self._base_cv_resolver = base_cv_resolver
```
- Gate: `if self._base_cv_resolver is not None and (await stages._read_base_structure(
  await self._base_cv_resolver(None))) is None:` — identical announce/wait/continue body and
  identical message text. Inert when the resolver is `None` (tests passing neither).
- Dispatch: `cv_structure_path=await self._base_cv_resolver(job.base_cv_id)` at the
  `run_stage` call. `_run_one` is already async and already holds `job`.

**Fallback semantics** (all in `resolve_path`, all logged, none fatal):
- assigned deck missing (deleted mid-flight, or a stale id) → default deck, warning;
- default deck missing → first deck in index order with a file;
- nothing at all → `None` → the gate blocks (jobs stay `pending`, never `failed`), and if a
  job somehow dispatches, `_read_base_structure(None)` returns `None` and the CV block is
  simply omitted — today's behavior for a missing structure.
- A *corrupt* non-default deck still yields `None` from `_read_base_structure` at stage time
  (existing tolerant read) → that job runs with no CV block. Same as today for a corrupt
  single file at the non-gating position. One known rough edge, not new surface: if the
  default deck goes corrupt, every job sits `pending` and assignment is `queued`-only — the
  fix is the editor, which is exactly what the gate banner tells the user.

**`jsa/cli.py::_bootstrap_cv_structure`** — deck-aware, same one-shot semantics and same
printed copy: "already exists" is now `cv_decks.resolve_path(settings, None) is not None`,
and the seed writes into a newly created deck via `create_deck` + `save_deck` instead of
`cv_structure.save`. The `--cv` flag's help text stays accurate with one word changed
("the CV structure" → "your first base CV").

**One existing test must be updated, intentionally** —
`test_cv_source_of_truth.py::TestBootstrapCvStructure::test_seeds_and_saves_when_missing`
asserts `settings.cv_structure_path.exists()` after the seed, and the seed now writes
`cv_decks/<id>.json` instead. Rewrite it to assert the deck file / `cv_decks.load_deck`.
This is an expected red, not an implementation bug — **do not "fix" it by dual-writing the
legacy file** (that would resurrect the second source of truth decision 4 removes). Its
siblings (`test_ignores_cv_when_structure_already_exists`,
`test_proceeds_with_no_cv_and_no_structure`, `test_infer_error_exits_nonzero`) stay valid
as-is.

### Parity gate — must pass before Phase 5 starts
`tests/backend/test_cv_decks_parity.py` (new). The **oracle is the pre-change behavior of a
legacy install**: with only `cv_structure.json` present and no index,

- the `fit_assessment` user message, and
- the `cv_adjust` initial user message

must be **byte-identical** to what `main` produces. Capture both with the existing
`_CapturingBackend` idiom from `tests/backend/test_stages.py::TestCvAdjustConsumesBaseStructure`
and `test_fit_assessment.py::TestFitAssessmentConsumesBaseStructure`.

**Ordering matters — this is Phase 4's step one, before any edit.** The worktree's initial
commit *is* `main`, so: write the parity test and generate the two golden strings there
first, commit them as fixture files, and only then start changing `orchestrator.py` /
`server.py` / `cli.py`. Capturing the oracle after the change would pin nothing.

Also pin:
`Orchestrator(cv_structure_path=...)` with no resolver reproduces today's gate exactly
(`test_cv_source_of_truth.py::TestOrchestratorCvGate` must pass unmodified, including its
present-but-corrupt case).

### Tests — `tests/backend/test_cv_decks_pipeline.py` (new)
A job with `base_cv_id` set gets that deck's content in its prompts (two decks with
distinguishable `contact.name`, assert on the captured message); unassigned → default deck;
unknown id → default + warning logged; gate blocks with zero decks and unblocks after a deck
save + `kick()`; `--fit-model` fit backend sees the same resolved deck.

---

## Phase 5 — Editor: deck rail

**Model:** `opus` for `editorStore.ts` (switch/flush/save orchestration), `sonnet` for the
`DeckRail.tsx` markup.

### Current state
- `frontend/src/editorStore.ts` (700 lines, zustand): one CV, `load()`/`reset()`/`save()`,
  history via `commit()` with a 600ms `COALESCE_MS` typing debounce and `HISTORY_CAP = 120`,
  plus `inferring`/`inferStep`/`inferError` and `startBlank()`.
- `save()` (`editorStore.ts:670`) is called only from `CvEditor.tsx`'s COMMIT button — **not**
  per keystroke — and PUTs `api.saveCvStructure`.
- `CvEditor.tsx:301` mounts and unconditionally fetches the one CV; `CvEditor.tsx:534` picks
  `EmptyState | BlocksView | DocumentView | SplitView`; the JSON drawer is an `<aside>`
  sibling of `<main>` at `CvEditor.tsx:544`.
- `frontend/src/theme/Icon.tsx` has `doc, cols, copy, trash, plus, x, check` — **no `pencil`,
  no `star`**.

### Desired state
The rail from the hand-off: 26px collapsed strip + 196px absolute hover flyout (no layout
shift), deck rows with active accent bar and hover icon cluster, dashed "NEW BASE CV" footer,
dimmed + non-interactive while `inferring`.

### Problems
- A new deck cannot be persisted until it validates → the switch flow needs the confirm
  path (user decision above).
- Server-side persistence means the rail's row labels (`auto_title`) go stale relative to the
  live editor buffer — the hand-off's `deckLabel` reads live `state.cv` for the active deck.
- `api.ts` error handling is a string-parsing idiom (`^HTTP \d+:\s*(.*)$` + JSON-detail
  unwrap) currently duplicated twice inside `editorStore.ts`; a third and fourth copy is not
  acceptable.

### Solutions

**`frontend/src/api.ts`** — deck functions beside the existing CV ones, same `apiFetch`
idiom: `listCvDecks`, `getCvDeck(id)` (404 → `null`, like `getCvStructure`),
`createCvDeck(name)`, `saveCvDeck(id, cv)`, `patchCvDeck(id, body)`, `duplicateCvDeck(id, name)`,
`deleteCvDeck(id)`.

**`frontend/src/editorStore.ts`** — extract the HTTP-detail parser into a module-level
`detailOf(err): string` helper (replacing both existing copies), then add:

```ts
decks: DeckDTO[]; defaultDeckId: string | null; activeDeckId: string | null;
railOpen: boolean; renameId: string | null; renameDraft: string; deckBusy: boolean;
```
- `hydrateDecks()` — `listCvDecks()`; active = `default_id ?? decks[0]?.id ?? null`; then
  `getCvDeck(active)` → `load(cv)`, else `reset()`. Replaces `CvEditor.tsx:301`'s effect.
- `save()` — PUTs to `saveCvDeck(activeDeckId, ...)`. When `activeDeckId` is `null` (fresh
  install, or right after "NEW BASE CV") it first `createCvDeck(null)` and adopts that id —
  this is the hand-off's `installCv` "create a slot if none is active" rule. On success it
  refreshes the index (auto-title/has_cv changed) and keeps the existing
  `useStore.setCvStructureExists(true)` call.
- `flushAndPersist(): Promise<boolean>` — the shared pre-navigation step used by switch /
  new / duplicate: `commit()`, then if `dirty` call `save()`; on failure show
  `window.confirm(t("cvDecks.discardDraft"))` and return its result.
- `switchDeck(id)` — no-op if already active; `flushAndPersist()` gate; then load the target
  (`load(cv)` resets history to that deck's own single baseline, per the hand-off), `view →
  "blocks"`, close the JSON drawer, clear `selectedId`/`saveError`.
- `newDeck()` — `flushAndPersist()` gate; `createCvDeck(null)`; adopt it as active; `reset()`
  so the existing `EmptyState` (RUN INFERENCE / INIT BLANK) re-shows for the new slot. The
  rail stays visible and every other deck stays reachable.
- `renameDeck(id, name)` (empty string → `null`, i.e. back to the auto title),
  `setDefaultDeck(id)`.
- `duplicateDeck(id)` — sends `name = "<current label> " + t("cvDecks.copySuffix")`, then
  switches to the new deck. **It must not use `flushAndPersist()`'s confirm path**: the
  server copies from the deck *file*, so a discarded invalid draft would silently duplicate
  stale content (and the hand-off duplicates the live buffer). Rule: if the source is the
  active deck and the buffer is dirty, `save()` it; if that save fails, **refuse the
  duplicate** and leave the existing inline `saveError` on screen — no prompt, nothing
  copied.
- `deleteDeck(id)` (re-hydrate; if the active deck was deleted, load the new default, or
  `reset()` + `activeDeckId = null` when none remain).
- `deckLabel(d)` — `d.name ?? (d.id === activeDeckId ? cv?.contact.name : d.auto_title) ??
  t("cvDecks.untitled")`, so the active row tracks the live buffer.

**`frontend/src/components/cv-editor/DeckRail.tsx`** (new, `EDITOR_THEME`) — the hand-off's
geometry verbatim: strip `width: 26`, `T.subtle`, `borderRight: 1px solid T.bd`, 13px `cols`
icon (accent while open) + a `writingMode: "vertical-rl"` "BASE CVs" label in
`Share Tech Mono` 9.5px / `.12em`; flyout `position: absolute; left: 26; top: 0; bottom: 0;
width: 196`, `T.shadowMd`, reusing the existing `cvfade .12s ease` animation. Header shows
the label + deck count; footer is the dashed `T.bd2` ghost "NEW BASE CV" with a `plus` icon.
Rows: `position: relative`, active → `T.surface` + `1px solid T.aBorder` + a 2px `T.a` bar
(`boxShadow: 0 0 8px T.a`, inset 6px); hover reveals **four** 20px icon buttons —
`star` (set default), `pencil` (rename), `copy` (duplicate), `trash` (delete, `T.danger`,
hidden when only one deck remains). The default deck's `star` is always visible in `T.a`.
Rename is an inline autofocus input, committed on Enter/blur, cancelled on Escape (a local
`onKeyDown`, not a global handler — the codebase has no global Escape idiom). Whole rail:
`opacity: .45; pointerEvents: "none"` and hover-disabled while `inferring`, so a deck switch
can never race an in-flight inference into the wrong slot.

Four icons + an ellipsised name inside 196px is tight but fits (icons are hover-only).

**`frontend/src/theme/Icon.tsx`** — add `pencil` and `star` to `IconName` + `PATHS`,
stroke-based, `viewBox 0 0 16 16`, `stroke-width 1.55`, round caps/joins. The hand-off's
pencil path is `M10.6 2.8a1.6 1.6 0 0 1 2.3 2.3L5.6 12.4l-3.1.8.8-3.1z` + `M9.4 4l2.3 2.3`.

**`frontend/src/components/cv-editor/CvEditor.tsx`** — mount effect calls
`st.hydrateDecks()`; render `<DeckRail />` as the first child of the main flex row, sibling
**before** `<main>`.

### Tests
- `frontend/src/__tests__/editorStore.test.ts` (extend): hydrate picks the default deck;
  `save()` targets the active deck; `save()` with no active deck creates one first;
  `switchDeck` persists a valid dirty buffer and loads the target; `switchDeck` with an
  invalid dirty buffer prompts and honors cancel/OK (stub `window.confirm`); `deleteDeck` of
  the active deck lands on the new default; `duplicateDeck` on a dirty-and-invalid active
  buffer never calls `api.duplicateCvDeck` and leaves `saveError` set.
- `frontend/src/__tests__/DeckRail.test.tsx` (new): renders one row per deck, marks the
  active row, hover cluster hides `trash` at one deck, rename commit/Escape, locked while
  `inferring`, "NEW BASE CV" calls `newDeck`.

---

## Phase 6 — Job row trigger + base-CV picker

**Model:** `sonnet`.

### Current state
- `JobList.tsx` renders `JobRow` inline (lines 44–133); the only per-row control is
  `state === "queued" ? <LaunchButton/> : <StatusBadge/>` at line 114. There are **no**
  per-row popovers today.
- The app's popover idiom is `useOutsideClick(ref, open, close)`
  (`frontend/src/hooks/useOutsideClick.ts`) + `panelBase(T, {chamfer})` + `T.shadowMd`.
  There is no Escape handling anywhere, and no transparent full-screen backdrop for anchored
  panels (that's reserved for `UnfitModal`).
- `JobList`'s `<nav>` is `overflow: "auto"` and `panelBase` applies a `clip-path` under the
  live `hud` skin, so an `absolute` panel on a row **will** be clipped — `Header.tsx:43-49`
  documents exactly this trap. `ChatBox.tsx`'s `MentionDropdown` (lines 254–362) is the
  in-repo reference for `position: "fixed"` + viewport clamping.

### Desired state
A 22×22 doc-icon trigger on every `queued` row (accent-tinted when assigned), opening a
280×360 fixed, clamped popover listing every deck with a check on the assigned one, a
`DEFAULT` tag, an empty state that opens the Structure Editor, and an UNASSIGN footer.

### Solutions

**`frontend/src/types.ts`** — `JobDTO.base_cv_id: string | null`; `CvDeckDTO { id; name:
string | null; auto_title: string | null; has_cv: boolean; is_default: boolean }`.

**`frontend/src/store.ts`** — `cvDecks: CvDeckDTO[]`, `cvDecksDefaultId: string | null`,
`cvPickerJobId: string | null`, `cvPickerPos: {x: number; y: number}`; actions
`hydrateCvDecks()`, `openCvPicker(job, e)` (no-op unless `state === "queued"`; clamps from
the click point with the `MentionDropdown` math), `closeCvPicker()`,
`assignBaseCv(jobId, deckId | null)` (calls `api.putJobBaseCv`, patches the job in the store,
closes the popover, toasts on failure via the existing toast helper).
`hydrateCvDecks()` runs on app mount and whenever `setEditorOpen(false)` fires — the
hand-off's "reload the list every time the Structure Editor closes".

**`frontend/src/components/BaseCvPicker.tsx`** (new) — mounted in `App.tsx` next to
`<Toast/>`, store-driven, `position: "fixed"` at `cvPickerPos`, `width: 280`,
`maxHeight: 360`, `panelBase(T, {chamfer: 12})` + `T.shadowMd` + the existing `jsfade`
animation, dismissed via `useOutsideClick` and the header × button. Header: `doc` icon in
`T.a`, title from `baseCvPicker.title`, subtitle `company — role` in mono `T.ink3`.
**The list is `cvDecks.filter(d => d.has_cv)`** — an empty, never-saved slot is not
assignable (Phase 3 rejects it with a 422), so offering it would be a trap; if that filter
leaves nothing, render the same empty state. Rows:
`doc` icon + label + a mono `DEFAULT` tag on the default deck + `check` in `T.a` when
assigned, row background `T.aSoft` when assigned. Empty state: `baseCvPicker.empty` +
a ghost "OPEN STRUCTURE EDITOR" calling `setEditorOpen(true)`. Footer, only when assigned:
full-width UNASSIGN.

**`JobList.tsx`** — the `queued` branch becomes a flex row (`gap: 6`) of
`<BaseCvTrigger job={job} />` then `<LaunchButton jobId={job.id} />`. `BaseCvTrigger` is a
small local component in `JobList.tsx` (it is row chrome, not a shared widget): 22×22,
`border: 1px solid ${has ? T.aBorder : T.bd}`, `background: has ? T.aSoft : "transparent"`,
icon `doc` at 12 in `has ? T.a : T.ink3`, `onClick` → `openCvPicker(job, e)` with
`stopPropagation()` so the row's own select handler doesn't fire. **Leave the flex row's
middle slot free** — the syringe lands between the two per the hand-off's ordering.

**i18n** — new keys in `frontend/src/i18n/strings.en.json` only, then
`./scripts/translate-ui.sh` (incremental) to fan out to the 19 other locales:
`cvDecks.railLabel`, `.newDeck`, `.rename`, `.duplicate`, `.delete`, `.setDefault`,
`.default`, `.untitled`, `.copySuffix`, `.discardDraft`, `.deckCount`;
`baseCvPicker.title`, `.empty`, `.openEditor`, `.unassign`, `.assign`, `.change`,
`.default`. No hardcoded literals in JSX — everything through `useT()`.

### Tests
- `frontend/src/__tests__/JobList.test.tsx` (extend): trigger renders on `queued` only (not
  `pending`/`running`/`review`); assigned styling; click opens the picker without selecting
  the row.
- `frontend/src/__tests__/BaseCvPicker.test.tsx` (new): lists decks with labels, **omits a
  `has_cv: false` slot**, marks the assigned one, `DEFAULT` tag placement, assign →
  `api.putJobBaseCv` + store patch, UNASSIGN sends `null`, empty state opens the editor,
  outside click closes.

---

## Phase 7 — Docs, build, review

**Model:** `sonnet`.

- **`CLAUDE.md`** — rewrite the "CV structure — single source of truth" section into
  "Base CVs (decks) — single source of truth", recording: the decks store shape, the
  resolved-path-into-`cv_structure_path` decision **and its rationale** (so nobody
  "simplifies" it into a `run_stage` signature change), the index-as-cache rule, the
  `queued`-only assignment rule, the delete-clears-undispatched-only rule, the
  legacy-file-is-inert-and-never-deleted rule, and the fact that `Job.base_cv_id` is
  read live at stage time (not snapshotted at launch).
- **`README.md`** — update the "CV Structure Editor" section (line ~358) and the endpoint
  table (line ~143) with the deck routes; note `--cv` now seeds the *first* base CV.
- **`npm run build`** in `frontend/` — the `jsa` CLI serves the gitignored `jsa/static`
  bundle, so a UI change is invisible until this runs.
- **`/code-review medium --fix`** (Sonnet, per CLAUDE.md's routine post-implementation pass).

---

## Verification

Run from the worktree unless noted.

1. **Backend suite** — `python -m pytest -q -m "not integration"`. Everything must pass,
   including the untouched `test_cv_structure.py`, `test_cv_source_of_truth.py`,
   `test_stages.py`, `test_fit_assessment.py`, and `test_prompt_prefix_stability.py`.
2. **Parity gate** — `python -m pytest tests/backend/test_cv_decks_parity.py -v`. Golden
   fixtures must be generated on `main` before Phase 4 lands.
3. **Frontend** — `cd frontend && npm install && npm test`, then
   `./scripts/translate-ui.sh --check` (catalogs in sync) and `npm run build`.
4. **Legacy-install migration, by hand** (the highest-value manual check):
   ```bash
   cp -r ~/.jsa ~/.jsa.bak            # snapshot first
   ls ~/.jsa                          # cv_structure.json present, no cv_decks.json
   # start the server (from the MAIN checkout after merge, per the pip -e hazard)
   curl -s localhost:8765/api/cv-decks | jq
   # -> exactly one deck, is_default true, auto_title = your contact name
   ls ~/.jsa/cv_decks/ && ls -l ~/.jsa/cv_structure.json   # legacy file untouched
   curl -s localhost:8765/api/config | jq .cv_structure_exists   # true
   ```
5. **End-to-end, two decks** —
   - In the editor: rail shows the migrated deck; "NEW BASE CV" → RUN INFERENCE on a second
     resume → COMMIT. Rename it, star it as default, duplicate it, delete the duplicate.
   - Launch flow: `jsa --csv <jobs.csv> --no-browser` (the `/run` skill has the standard
     invocation). On a `queued` row, click the doc icon, assign deck B, LAUNCH.
   - Confirm the model actually got deck B: `sqlite3 ~/.jsa/jsa.sqlite "select base_cv_id
     from jobs where id='<id>'"`, then inspect the first `fit_assessment` user `Message` row
     for deck B's contact name (`select content from messages where job_id='<id>' and
     stage='fit_assessment' and role='user'`).
   - Assign nothing on a second job → its prompt must carry the **default** deck.
6. **Gate behavior** — delete every deck via the API with a `queued`+launched job present:
   jobs stay `pending` (never `failed`) and the gate banner appears; save a deck → the job
   dispatches without a restart.
7. **Visual check** — Playwright is not installed here; the `claude-in-chrome` skill can
   drive the two screens against `npm run dev` (`http://localhost:5173`) if a visual pass is
   wanted. Otherwise check by hand: rail hover-open causes **no layout shift**, the rail is
   dimmed during inference, and the picker is not clipped by the job-list scroll container.

---

## Change Log

_(Entries are appended as phases land: **YYYY-MM-DD**: context, actions, decisions,
verification result.)_

**2026-09-04**: Note — Phases 1-5 (deck store, deck HTTP API + `Job.base_cv_id` +
assignment endpoint, orchestrator resolution/gate, editor deck rail) landed in prior
sessions (commits `3351f37`..`fe0cb52` on `feat/cv-decks`) without a changelog entry here;
recorded retroactively for continuity, not written by the session that did that work.

**2026-09-04**: Context — implement Phase 6 (job row trigger + base-CV picker) per the
locked plan. Actions — added `JobDTO.base_cv_id` and `api.putJobBaseCv` (frontend);
extended `store.ts` with `cvDecks`/`cvDecksDefaultId`/`cvPickerJobId`/`cvPickerPos` state
and `hydrateCvDecks`/`openCvPicker`/`closeCvPicker`/`assignBaseCv` actions, wired
`hydrateCvDecks()` into app mount and into `setEditorOpen(false)` (picks up
create/rename/delete from the Structure Editor without a reload); added a `BaseCvTrigger`
(`role="button"` span, not a nested `<button>`, mirroring `LaunchButton`) to `JobList`'s
queued-row footer, leaving the middle flex slot free for `feat/prompt-injection`'s syringe
trigger per the collision map; added `BaseCvPicker.tsx` (fixed, viewport-clamped popover,
`useOutsideClick`-dismissed, filters out `has_cv:false` slots, DEFAULT tag, UNASSIGN
footer, empty-state CTA into the Structure Editor); extended `Toast`/`ToastItem` with a new
`"error"` kind (verbatim server-detail message, not an interpolated template) so a failed
assignment surfaces without inventing a second notification surface; exported
`editorStore.detailOf` for reuse instead of a third copy; added `baseCvPicker.*` i18n keys
(English only — batch translation deferred to Phase 7, matching Phase 5's precedent).
Decisions — anchored the trigger's click point via `getBoundingClientRect()` rather than
the raw pointer event, since a keyboard `Enter`/`Space` activation (required for the
`role="button"` span, which gets no native keyboard handling) has no `clientX`/`clientY` of
its own. Verification — verified: `npx tsc --noEmit` clean, `npx vitest run` 402/402 passing
across 26 files (new: `BaseCvPicker.test.tsx`; extended: `JobList.test.tsx`, `Toast.test.tsx`,
`store.test.ts`, plus a one-line `mobile_responsive.test.tsx` mock fixup), `npm run build`
clean. Backend suite (`pytest`) could not be run to confirm no regression — this
environment's global Python has `pytest-asyncio` uninstalled and `jsa`'s editable install
currently points at an unrelated worktree (`agent-a26487e88e4a2e806`), a pre-existing,
environment-wide issue unrelated to this phase (Phase 6 touched no Python file); needs user
action to resolve the shared Python environment before the backend suite can confirm green
here.

**2026-09-04**: Context — `/code-review low` on the full `feat/cv-decks` branch diff (Sonnet,
findings-only, no `--fix`) surfaced 4 findings, mostly in Phases 1/5 code. User asked to
apply two of the debatable ones: add a `confirm()` before deck delete, and add a backend
409 last-deck guard. Actions — added `window.confirm(t("cvDecks.confirmDelete", ...))` to
`DeckRail.tsx`'s `onDelete` handler (rail's trash icon), matching the existing
discard-draft-on-switch idiom already in `editorStore.ts`; added the `cvDecks.confirmDelete`
i18n key; added two `DeckRail.test.tsx` tests (confirm → deletes, cancel → does not) and
stubbed `window.confirm` in the pre-existing `it.each` leak-check test to keep it
console-clean. Drafted the backend guard in `routes_cv_decks.py::delete_cv_deck` (409 when
`len(index.decks) == 1`), then discovered it directly conflicts with two existing,
deliberately-written tests (`test_cv_decks_api.py::TestKickWiring::test_delete_kicks` and
`::TestConfigSignal::test_toggles_false_true_false_across_create_save_delete`) that pin
"delete your only deck → back to zero decks, gate blocks dispatch" as correct, tested
Phase 4 behavior (`Orchestrator`'s CV-structure gate tolerates zero decks: jobs stay
`pending`, never `failed`). Decisions — escalated to the user rather than silently
rewriting those two tests; user chose to drop the backend guard and keep only the
`confirm()` fix — the rail's `canDelete={decks.length > 1}` UI restriction remains the sole
friction against reaching zero decks, matching the locked Phase 4 design.
`routes_cv_decks.py` reverted to byte-identical with its last commit (`git diff` empty).
Verification — verified: `npx vitest run` 405/405 passing across 26 files (new count vs.
Phase 6's 402, from the 2 new DeckRail tests + 1 stubbed leak-check case), `npx tsc --noEmit`
clean, `npm run build` clean. Backend suite still unrunnable in this environment for the
same pre-existing reason noted in the Phase 6 entry above — not re-verified here since the
backend diff is a no-op (file reverted to HEAD).

**2026-09-04**: Context — implement Phase 7 (docs, build, review) and wrap up the plan.
Actions — rewrote CLAUDE.md's "CV structure — single source of truth" into "Base CVs
(decks) — single source of truth", written against the *shipped* code rather than the
plan text, so it also records the three post-Phase-4 fixes the plan predates
(`b37419e` per-event-loop lock keying, `2d849c5` unreadable-index tolerance, `5b221ba`
atomic `ensure_default_deck`) plus the review-triage decision that **zero decks is a
legal state with no backend last-deck guard**, naming the two tests that pin it; fixed
the now-stale "CV structure gate" cross-reference further down the same file. Updated
README (deck routes in the endpoint table, deck rail / per-job picker / legacy-migration
behaviour in the CV Structure Editor section, `--cv` seeds the *first* deck, repo-layout
lines). Ran the i18n fan-out deferred from Phases 5 **and** 6 — `--check` showed 19 keys
missing across all 19 non-English locales; `zh` failed once on a malformed model reply
and was re-run; `--check` is now clean. Rebuilt the frontend bundle. Ran
`/code-review medium feat/cv-decks` (findings-only) and triaged its 4 findings: applied
3, escalated 1.
Decisions — (a) the plan's `/code-review medium --fix` was run **without** `--fix`:
CLAUDE.md forbids it and postdates this plan, so triage stayed with this session.
(b) Applied findings 1-3 with a regression test each, and **verified every one of those
tests fails against the pre-fix code** — the first version of the `deckBusy` test passed
either way (the second call was dying on an unmocked save rather than on the guard) and
was rewritten around a clean buffer plus a synchronous flag assertion before it was
trusted. (c) **Escalated finding 4** rather than acting: it argues
`clear_base_cv_assignments`' `queued`+`pending` scoping is wrong because a BF-19 backend
switch or model-ladder hop rewinds a `cv_adjust` failure to `pending` and re-runs the
stage, so a job whose deck was deleted mid-flight is genuinely rebuilt against the
default deck while `base_cv_id` still records the deleted one. The premise is correct,
but the fix reopens the user's own locked 2026-09-04 decision, so it is the user's call —
left unactioned and unrecorded in CLAUDE.md pending that decision.
Verification — **verified**, and this corrects the two prior entries' claim that the
backend suite is unrunnable here and "needs user action": it is runnable via the project
venv (`/Users/wiam/VSCodeProjects/JSA/.venv/bin/python -m pytest`, run from the
worktree, which resolves `jsa` to the worktree). Only the `python3` on PATH lacks
`pytest-asyncio`. Backend `1882 passed, 2 skipped` (`-m "not integration"`), including
`test_cv_decks_parity.py` — the plan's named parity gate, never confirmed green since
Phase 4 — and all five must-stay-untouched files. Frontend `npx vitest run` 410/410
across 26 files, `npx tsc --noEmit` clean, `npm run build` clean,
`translate-ui.sh --check` clean. **Not** done: the manual end-to-end checks (Verification
steps 4-7 — legacy-install migration, two-deck launch flow, gate behaviour, visual pass),
which need a live server from the main checkout after merge. Note: an untracked
`.claude/plans/CV_DECKS_PLAN.md` sits in the worktree; it was not created by this session
and has been left in place.

**2026-09-04**: Context — the user asked whether a base CV could be locked against
*deletion* while jobs are still using it (editing to stay unrestricted), releasing when a
job graduates to `approved` or is dismissed/deleted. This is scope beyond the plan, taken
deliberately because it supersedes review finding 4 rather than sitting beside it.
Actions — traced the actual re-read path first: the base CV is read only when a stage
opens a **fresh** session (`stages.py`'s fresh-vs-resume branch keys off whether `Message`
rows exist), so a mid-flight job is immune to its deck file vanishing until something
wipes its Messages — which `backend_switch_reset` does on a BF-19 switch or model-ladder
hop, rewinding `cv_adjust` to `pending`. Added `repo.DECK_LOCK_STATES` +
`count_jobs_holding_deck`/`count_jobs_holding_decks` (one grouped query for all decks),
a 409 on `DELETE /api/cv-decks/{id}`, `in_use_by` on the deck DTO, a disabled-with-reason
trash icon in `DeckRail`, the `cvDecks.deleteLocked` key fanned out to all 19 locales, and
CLAUDE.md/README updates. Decisions — (a) holding states are `pending` through `failed`;
`pending` holds because it is the rewind's landing state and `failed` holds because a
re-run re-dispatches into a fresh session, both chosen by the user from the options put to
them. (b) Editing left ungated on purpose — a started job has the CV in its `Message`
rows, so a PUT cannot reach it. (c) `pending` kept in `clear_base_cv_assignments`' WHERE
clause even though the lock makes it unreachable through the route: it closes the
count→`delete_deck` window where a `queued` job can be LAUNCHed. (d) Checked before
building that this does **not** collide with `test_delete_kicks` /
`test_toggles_false_true_false_across_create_save_delete` — those create no jobs, and this
gates on *references*, not deck count, so the rejected last-deck guard is not resurrected
and zero decks stays legal. One pre-existing route-level test
(`test_delete_clears_undispatched_and_spares_dispatched`) did encode the old contract and
was updated with the reason recorded in its docstring; the repo-level test pinning the
`pending` clear directly was left untouched. Verification — verified: backend
`1900 passed, 2 skipped`, frontend 412/412 across 26 files, `npx tsc --noEmit` clean,
`npm run build` clean, `translate-ui.sh --check` clean. Manual end-to-end (plan
Verification steps 4-7) still outstanding and now has one more case worth covering: launch
a job on deck B, try to delete deck B (expect the rail's trash disabled + a 409), approve
the job, delete again (expect success).

---

## Decisions Log

_(Reserved for the user. Not to be written by any agent.)_
