---
status: InProgress
---

# Per-Job Prompt Injection ("PROMPT_INJECTOR")

## Context

**Source:** `~/Downloads/prompt_injection_feature.zip` → `design_handoff_prompt_injection/`
(`README.md` + `JSA App Shell.dc.html`, a design-reference prototype — not code to copy).

**Problem.** Today every job runs the exact same file-authored system prompt. There is no
per-job lever: if the user knows *this* application needs "lead with the payments work" or
"never hedge", the only options are editing `jsa/prompts/*.md` globally (which affects every
job) or answering a follow-up question after the model has already committed to a direction.

**What we're building.** A syringe icon on each pre-launch job row opens a small floating
panel ("vial") where the user attaches three optional overrides to that job:

| Field | Destination |
|---|---|
| **PRE-FIX PROMPT** | inserted *before* the stage's system prompt |
| **POST-FIX PROMPT** | appended *after* the stage's system prompt |
| **FIRST USER MESSAGE** | appended to the initial user message of a fresh session |

Plus a globally-scoped library of named presets ("doses") that can be pasted into any job.

**Outcome.** A user can hand-tune a single application's prompt before spending a single
token on it, and reuse that tuning across jobs — without touching the shared prompt files
and without any effect on jobs that opt out.

**Folded-in scope (unrelated, small).** Phase 5 adds a right-aligned "JOB POSTING" link
button to the review-stage tab row, opening the CSV-ingested posting URL in a new tab.

### Decisions locked (confirmed with the user)

1. **Trigger state = `queued`, not `pending`.** The prototype's `pending` is *the state that
   renders the LAUNCH control* (`JSA App Shell.dc.html:695`), which in this repo is `queued`
   (`JobList.tsx:114`). `pending` is post-launch here — the orchestrator can dispatch it
   between a GET and a PUT, so "pre-launch only" would be a lie. Editing is gated on `queued`
   in the UI **and** enforced with a 400 in the API.
2. **Fit gate: system prefix/postfix yes, first-user-message no.** `fit_assessment` gets the
   user's tone/framing wrapper (it is the same job), but the FIRST USER MESSAGE field appends
   only to `cv_adjust`/`cover_letter` fresh sessions, so a data nudge ("mention 6 years of
   Rust") cannot fabricate a FIT verdict.
3. **Presets live server-side**, in a new `jsa/store/injection_presets.py` mirroring
   `preferences.py` / `backend_models.py` exactly — not `localStorage`. Hand-authored presets
   should not die with browser data. (CLAUDE.md already documents `backend_models.py` as
   "mirrors `preferences.py`'s pattern exactly"; a third store is the expected move.)
4. **`vial` variant only, `cursor` placement only.** The handoff explores four interchangeable
   chromes; `vial` is its stated default. This repo has no `injectorStyle` / `injectorPlacement`
   prop plumbing, so `hud`/`rail`/`ampoule` and the "center" placement mode would be dead code
   with no toggle to reach them. **Assumption, stated up front — not silently dropped scope.**
5. **Wire shape is snake_case** — `{prefix, postfix, first_msg}`, not the prototype's
   `firstMsg`. Every DTO here is snake_case (`current_stage`, `fit_reason`, `suggested_replies`).

### The architectural crux — and exactly how much of it is real

CLAUDE.md → "Prompt caching (HTTP API backends)" states a **cross-job system-prefix
invariant**: `assemble_system_prompt` sees only the prompt file, language, schema and
calendar day — *no per-job bytes ever* — so every job at a given (stage, language, mode) on a
given day sends a byte-identical system prefix.

Caching here is a pure prefix match with **exactly one breakpoint**, at the end of the system
block (see that section's per-backend table). So the precise cost is narrower than "this
breaks prompt caching":

| Reuse | Effect |
|---|---|
| **Intra-job** — a job's turn 1 → its turns 2, 3, 4… | **Unaffected.** The injection is frozen pre-launch and never edited, so an injected job's system prompt is byte-stable across its own turns; turn 1 writes an entry, later turns read it. |
| **Cross-job, uninjected → uninjected** | **Unaffected.** `injection=None` (and all-blank, normalized to `None`) is byte-identical to today, so uninjected jobs keep sharing one entry exactly as now. An injected job running between two uninjected ones does **not** evict it — entries are keyed by prefix, not a single slot. |
| **Cross-job, into an injected job** | **Forfeited, deliberately.** An injected job can't read the shared entry and writes its own instead. |

Measured cost of that one forfeited case: the assembled system prompts are ~4.4k tokens
(`cv_adjust`), ~2.9k (`cover_letter`), ~1.4k (`fit_assessment` — already below every
provider's minimum cacheable size, so it never cached anyway). Per injected job that is full
price on ~4.4k input tokens for turn 1 instead of a 0.1× cached read, plus the 1.25× write
premium on the entry it creates. Opt-in, per job, once.

We accept that trade rather than the alternative — routing prefix/postfix into the user
message to preserve sharing — because that would make two of the three fields functionally
identical and make the panel's own helper text (*"Inserted before the system prompt"*) a lie.
The invariant is **narrowed, not abandoned**, and the parity gate is **strengthened, not
edited**: see Phase 2.

**Rejected alternative — the two-block system split.** Even a postfix-only injection loses
the shared read, because the single breakpoint sits at the *end* of the system block, so any
difference inside it diverges there. Splitting the system into
`[shared prompt + runtime sections, cache_control]` + `[per-job injection, no cache_control]`
would recover the shared hit for postfix-only injections. Rejected: it works only on
`anthropic`, `openrouter` and `opencode-go`'s `/chat` (block-shaped system) — Mistral's
mechanism is a top-level `prompt_cache_key` hash and Gemini's is implicit whole-
`systemInstruction` matching, so neither benefits; a **pre**-fix is at position 0 by
definition and no breakpoint placement can save it; and it would force the postfix to sit
*after* the structured contract, the position Phase 2 deliberately reserves for the
machine-authored sections.

---

## Phase 1 — Storage and API for `job.injection`

### Current State
`Job` (`jsa/db/models.py`) has no per-job prompt override. `_job_to_dict`
(`jsa/api/routes_jobs.py:79`) exposes ~17 scalar fields. Migrations are `create_all` plus
one additive `ALTER TABLE` per column in `jsa/db/engine.py::init_db`, each in its own
`try/except OperationalError` (deliberately not shared — see the `model_name`/`model_hops`
comment at `engine.py:64`).

### Desired State
A nullable `jobs.injection` TEXT column holding either SQL `NULL` or a JSON object
`{"prefix", "postfix", "first_msg"}`; a typed parse/normalize helper; the value on the job
DTO; and a single write endpoint that refuses anything but a `queued` job.

### Problems/Bugs
- Three separate columns would let a half-written triple exist; the domain shape is
  "absent, or all three together". One JSON column matches it.
- Whitespace-only fields must not count as "has an injection" — otherwise a user who types a
  space into one box silently drops that job out of the shared prompt cache forever, and the
  syringe icon lights up for nothing. Normalization has to happen in **one** place, not at
  each read site.
- Prototype key `firstMsg` would be the only camelCase field in the whole API surface.

### Solutions

**`jsa/schema/injection.py`** (new) — the single normalization point:

```python
class PromptInjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = ""
    postfix: str = ""
    first_msg: str = ""

    def normalized(self) -> "PromptInjection | None":
        """Strip each field; return None when nothing survives."""
        p, s, f = self.prefix.strip(), self.postfix.strip(), self.first_msg.strip()
        if not (p or s or f):
            return None
        return PromptInjection(prefix=p, postfix=s, first_msg=f)


def parse_injection(raw: str | None) -> PromptInjection | None:
    """Job.injection column -> normalized model. Malformed JSON reads as None."""
```

`parse_injection` **must not raise** on a malformed blob — a hand-edited DB row must degrade
to "no injection", never hard-fail every stage of that job.

**`jsa/db/models.py`** — one column on `Job`:
```python
injection: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON {prefix, postfix, first_msg}; NULL = none
```

**`jsa/db/engine.py::init_db`** — its own try/except block, matching the existing style:
```python
try:
    await conn.execute(text("ALTER TABLE jobs ADD COLUMN injection TEXT"))
except OperationalError:
    pass
```

**`jsa/api/routes_jobs.py`**
- `_job_to_dict`: `"injection": parse_injection(job.injection)` — the parsed object (or
  `null`), never the raw string, so the frontend never parses JSON out of a JSON field.
- New `PUT /api/jobs/{job_id}/injection`, body `InjectionBody{prefix, postfix, first_msg}`
  (all defaulting to `""`), returning the full job dict like the other mutation routes:
  - 404 unknown job;
  - **400 unless `job.state is JobState.queued`** (message naming the actual state, matching
    `launch_job`'s existing 400 wording);
  - `body.normalized()` → `None` ⇒ store SQL `NULL` (this is the design's "all three empty
    clears the injection"); otherwise `model_dump_json()`.
- No state transition, so this is a plain field write + commit — **not** `repo.checkpoint`
  (CLAUDE.md's checkpoint rule covers writes that *change job state*; this one does not).

**Untouched, deliberately:** `reset_job`, `repo.backend_switch_reset`, `launch_all_jobs`. The
column simply rides the Job row through every reset and every backend/model hop, which is the
correct behavior — do not add it to any reset list.

**Tests** — `tests/backend/test_prompt_injection.py`: normalization (blank/whitespace → None),
malformed-JSON tolerance, DTO round-trip, 400-on-non-queued for each of `pending`/`running`/
`approved`, all-blank PUT clearing an existing injection, and injection surviving `reset_job`.

---

## Phase 2 — Prompt assembly and pipeline wiring (the crux)

### Current State
`assemble_system_prompt` (`jsa/pipeline/prompt_assembly.py:207`) composes
`prompt_text` → structured contract → language directive → current-date directive, from
**four** inputs, none of them per-job. It has three call sites in `jsa/pipeline/stages.py`:

| Line | Site | Covers |
|---|---|---|
| 771 | `resume_system_prompt` | every `restore_session` — `cv_adjust`/`cover_letter` resume, both `revising_*` branches |
| 893 | `fresh_system_prompt` | fresh `cv_adjust` / `cover_letter` |
| 1190 | inside `_run_fit_assessment` | the fit gate |

`_build_initial_user_msg` (`stages.py:1464`) builds `brief + cv_block + JD + TIER`.

Two gates pin the current behavior: `tests/backend/test_prompt_assembly.py` (84-case golden
fixture, byte-identical to the pre-refactor `_with_language_directive`) and
`tests/backend/test_prompt_prefix_stability.py` (within-process and cross-`PYTHONHASHSEED`
byte-stability, plus a cross-job identity class asserting two jobs with different JD/company/
role produce the *same* system prompt).

### Desired State
- `prefix + prompt_text + postfix`, then the runtime sections as an **untouched suffix**.
- `first_msg` appended to `_build_initial_user_msg`'s output only.
- The injection resolved **exactly once** per `run_stage` invocation and threaded to all
  three assembly sites from that one value.
- Both existing gates pass **unchanged and byte-identical**, and gain a new class proving the
  no-injection and all-blank cases still produce the shared prefix.

### Problems/Bugs

1. **Ordering is load-bearing, not cosmetic.** `_structured_contract` says *"This contract
   supersedes any sentinel-block instructions above."* A user postfix landing *after* it —
   e.g. "reply in plain prose, no JSON" — becomes the last word over the JSON contract. The
   result is `ProtocolError` → the `MAX_FINAL_CORRECTIONS` self-heal budget burned → a BF-19
   backend hop, on every turn. The machine-authored sections must keep final position.
   Bracketing `prompt_text` is also the faithful reading of the labels: "the system prompt"
   in the user's mental model is the prompt file.

2. **Thread it into one call site and not the others and the override silently vanishes
   mid-conversation.** Every structured-capable backend is wire-stateless and resends the
   system prompt on every HTTP call. Miss `resume_system_prompt` and the injection applies to
   turn 1 and evaporates from turn 2 onward — the *exact* failure shape CLAUDE.md already
   documents for the structured contract being dropped on resume (a `cv_adjust` job looping
   forever re-asking its opening question).

3. **The caching invariant as currently written forbids this outright.** Left unamended, the
   next agent reads "no per-job bytes … ever", sees the injection, and removes it as a
   violation. The doc has to be narrowed in the same change (Phase 5).

4. **A bare append to the initial user message is ambiguous.** `_build_initial_user_msg`'s
   output ends with `TIER: A`; appending raw user text there gives the model no way to tell
   where JD data ends and instruction begins.

### Solutions

**`prompt_assembly.py`** — new keyword-only param, defaulting to `None`:

```python
def assemble_system_prompt(
    prompt_text: str,
    *,
    language: str,
    structured_model: dict[str, Any] | None = None,
    fit_verdict: bool = False,
    for_resume: bool = False,
    now: datetime | None = None,
    injection: PromptInjection | None = None,   # NEW
) -> str:
```

The very first thing the function does:

```python
inj = injection.normalized() if injection is not None else None
base = prompt_text
if inj is not None:
    if inj.prefix:
        base = f"{inj.prefix}\n\n{prompt_text}"
    if inj.postfix:
        base = f"{base}\n\n{inj.postfix}"
```

…and every existing branch then reads `base` where it read `prompt_text`. Because the param
defaults to `None` and re-normalizes defensively, **`injection=None` and an all-blank
injection are both byte-identical to today's output** — both existing gates pass with zero
edits, including the `for_resume=True` sentinel-mode early return (which must return `base`,
not `prompt_text`, or a CLI resume drops the wrapper).

**`stages.py::run_stage`** — resolve once, immediately after the `language_code` block:

```python
# ONE resolution per invocation, threaded to every assemble_system_prompt call
# below. job.injection rides the Job row, so this is zero extra IO.
injection = parse_injection(job.injection)
```

Then pass `injection=injection` to **all three** sites: `resume_system_prompt` (771),
`fresh_system_prompt` (893), and — via a new parameter on `_run_fit_assessment` — the fit
site (1190). Name this in the code comment as a hard invariant, the way the
`structured_schema` / `adapt_history(structured=...)` pairing is already stated: *the
injection value passed to every assembly site in one `run_stage` call must come from a single
resolution.*

**First user message** — `_build_initial_user_msg` gains `first_msg: str | None = None`,
appended last, behind an explicit label so the boundary is unambiguous:

```
TIER: {job.tier}

ADDITIONAL INSTRUCTIONS FROM THE USER (apply these to your work on this application):
{first_msg}
```

Called from the fresh `cv_adjust`/`cover_letter` branch (`stages.py:892`) with
`injection.first_msg if injection else None`. **`_build_fit_user_msg` is not touched** —
locked decision #2.

**Two facts worth writing down so nobody re-derives them:**
- `_load_history` (`stages.py:1517`) filters to `role in ("user", "assistant")`, so the
  system row persisted at `stages.py:908` — which now contains the injected prompt — is
  structurally invisible to replay. **No double-injection risk on resume.**
- `job.injection` is a column on the already-loaded `Job`; read it off the passed-in object,
  never re-query.

**Tests**
- `tests/backend/test_prompt_injection.py` (cont.): prefix-before / postfix-after placement;
  postfix lands *before* the structured contract, language directive and date directive
  (assert relative index order explicitly); `injection=None` ≡ today byte-for-byte; all-blank
  ≡ today byte-for-byte; `for_resume=True` in sentinel mode still carries the wrapper;
  `_build_initial_user_msg` append; `_build_fit_user_msg` unchanged with an injection present.
- A `FakeAgentBackend`-driven `run_stage` test asserting the injection is present in **both**
  the fresh-session and the resumed-session system prompt (Problem 2's regression gate) —
  fakes, not mocks, per CLAUDE.md → "Testing conventions".
- **Extend** `tests/backend/test_prompt_prefix_stability.py` with a new class: two jobs with
  no injection still identical; two jobs with all-blank injections still identical *and*
  equal to the no-injection prefix; a job with a real injection deliberately differs (a
  documented, intended trade, asserted so it can't happen by accident).

---

## Phase 3 — Preset ("dose") store and routes

### Current State
Two single-file JSON stores exist with an identical shape: `jsa/store/preferences.py` and
`jsa/store/backend_models.py` — sync read/write wrapped in `asyncio.to_thread`, path derived
from `Settings.db_path.parent` (so a test with an isolated `db_path` is automatically
self-isolated), served by a tiny GET/PUT router registered in `jsa/server.py`.

### Desired State
A third store, `injection_presets.json`, holding a global (not per-job) ordered list of named
doses, reachable at `GET`/`PUT /api/injection-presets`.

### Problems/Bugs
- Presets are the *only* piece of this feature not tied to a job, so putting them on the Job
  row or in job state would be wrong; they need their own store.
- Naming must not collide: `Settings.injection_presets_path` alongside `preferences_path` /
  `backend_models_path` / `cv_structure_path`.

### Solutions

**`jsa/store/injection_presets.py`** (new) — a literal structural copy of `preferences.py`
(missing file → valid empty default, never 404):

```python
class InjectionPreset(BaseModel):
    id: str
    name: str
    prefix: str = ""
    postfix: str = ""
    first_msg: str = ""
    saved_at: str = ""     # ISO-8601, client-supplied; display only

class InjectionPresets(BaseModel):
    presets: list[InjectionPreset] = []

def injection_presets_path(settings) -> Path: ...
async def read(path) -> InjectionPresets: ...
async def load(settings) -> InjectionPresets: ...
async def save(settings, presets) -> None: ...
```

**`jsa/config.py`** — `injection_presets_path` property = `self.db_path.parent /
"injection_presets.json"`, next to the three existing derived paths.

**`jsa/api/routes_injection_presets.py`** (new) — whole-list PUT (the prototype's model:
add / delete / reorder are all just a new array), registered in `server.py` next to
`preferences_router`. A modest cap (e.g. 200 presets, 20 000 chars per field) rejected with
422, so a runaway client can't write an unbounded file.

**Tests** — `tests/backend/test_injection_presets.py`, mirroring `test_preferences.py`:
missing file → empty list, round-trip through save/load, PUT then GET via the API, cap
enforcement.

---

## Phase 4 — Frontend: syringe trigger + vial panel

### Current State
`JobList.tsx::JobRow` (line 55) is itself a `<button>`; its footer row renders
`job.state === "queued" ? <LaunchButton/> : <StatusBadge/>` (line 114). `LaunchButton.tsx`'s
header comment records the constraint that made it a `role="button"` `<span>`: nesting a real
`<button>` inside the row button is invalid HTML. `App.tsx` mounts overlays (`CvEditor`,
`Toast`, `ScratchBuffer`) as siblings above the layout row. `theme/Icon.tsx` has 36 glyphs;
ones needing non-`<path>` shapes (`server`, `blocks`, `cols`, `globe`, `search`, `play`) use
an empty `PATHS` entry plus a case in the `extraShapes` switch.

### Desired State
A 22×22 syringe toggle immediately left of the LAUNCH pill on `queued` rows, lit when an
injection exists; clicking it opens the vial panel at the click point.

### Problems/Bugs
- **The syringe is not a `d`-string glyph.** The design's icon is `<g transform="rotate(38 8 8)">`
  wrapping four paths *plus* a `<rect>` and a `<circle>` (`JSA App Shell.dc.html:576`). It
  must use the `extraShapes` escape hatch, not the `PATHS` array — the rotation group has to
  wrap everything, so the paths go inside `extraShapes` too and the `PATHS` entry stays `[]`.
- **A `<button>` trigger would produce invalid nested-button HTML.** Copy `LaunchButton`'s
  `role="button"` / `tabIndex` / `stopPropagation` / Enter-Space shape exactly.
- **Rendering the panel inside the row subtree breaks it.** The row is inside an
  `overflow-y: auto` aside; a `position: fixed` child there is clipped by ancestor transforms
  and stacking contexts. The prototype's `{{ injectorToast }}` mount is top-level for this
  reason — lift open-state to the store and render backdrop + panel as an `App.tsx` sibling,
  next to `<Toast/>`.
- **The clamp is a documented footgun.** The README calls out that a naive "reserve 80px from
  the bottom" clamp puts the footer buttons off-screen and unreachable. Port the formula
  verbatim.

### Solutions

**`frontend/src/theme/Icon.tsx`** — add `"syringe"` to `IconName`, `syringe: []` to `PATHS`,
and an `extraShapes` case returning the rotated group verbatim from the design.

**`frontend/src/types.ts`**
```ts
export interface PromptInjectionDTO { prefix: string; postfix: string; first_msg: string; }
export interface InjectionPresetDTO { id: string; name: string; prefix: string; postfix: string; first_msg: string; saved_at: string; }
// on JobDTO:
injection: PromptInjectionDTO | null;
```

**`frontend/src/api.ts`** — `putJobInjection(id, body)`, `getInjectionPresets()`,
`putInjectionPresets(list)`, following the existing call shapes.

**`frontend/src/store.ts`** — following `launchJob`'s shape (optimistic write, revert +
`console.error` on failure):
- `injectorJobId: string | null`, `injectorPos: {x, y}`, `injectionPresets: InjectionPresetDTO[]`
- `openInjector(jobId, x, y)` / `closeInjector()`
- `saveInjection(jobId, draft)` → PUT, then `upsertJob(returned)`
- `hydrateInjectionPresets()` (called once on boot alongside `hydrateLanguage`),
  `saveInjectionPresets(list)`
- Draft / save-name / clear state is **component-local** to `PromptInjector.tsx` — it is
  discarded on close by design, so it does not belong in the global store.

**`frontend/src/components/PromptInjector.tsx`** (new) — the vial, per the handoff:
380px wide, `maxHeight: 80vh`, `panelBase(T, {chamfer: 14})` + `cornerMarks(T, T.aBorder, 10)`
(both already in `theme/chrome.tsx`), `T.shadowMd`; header with syringe + `PROMPT_INJECTOR` +
`{company} — {role}` + the 3-bar fill indicator + close ×; scrollable body with three
3-row textareas, a divider, `SAVED_DOSES` chips and the save-as row; footer CLEAR / CANCEL /
SAVE INJECTION. A full-viewport transparent backdrop at `zIndex: 95` closes on click; the
panel sits at `zIndex: 96` and stops propagation.

Positioning, ported verbatim (note `w = 400` for a 380px panel — the extra 20px is the
design's intentional right margin; keep it):
```ts
const w = 400, panelH = Math.min(560, vh - 24);
x = Math.min(Math.max(12, clientX), vw - w - 12);
y = Math.max(12, Math.min(clientY, vh - panelH - 12));
```

Behaviors from the README: open loads the job's saved injection or blanks; CLEAR empties the
draft without closing and without touching what's persisted; SAVE with all three blank clears
the job's injection; preset save is ignored when the name is blank or all fields are empty and
prepends to the list; clicking a chip name overwrites all three draft fields without closing;
the chip's × deletes permanently. **One addition beyond the spec:** Escape also closes
(discarding the draft, same as Cancel) — standard for a dismissible overlay, one line.

**`frontend/src/components/JobList.tsx`** — replace line 114's `queued` branch with a 6px-gap
flex row: `<InjectTrigger job={job}/>` then `<LaunchButton/>`.

**`frontend/src/App.tsx`** — render `<PromptInjector/>` next to `<Toast/>`; it returns `null`
unless `injectorJobId` is set.

**i18n** (CLAUDE.md → "Frontend i18n"): every field label, helper text, placeholder, tooltip,
empty state ("No saved doses yet.") and button label goes into `strings.en.json` under
`promptInjector.*`, rendered via `useT()`. The code-like HUD literals — `PROMPT_INJECTOR`,
`SAVED_DOSES` — stay hardcoded under the deliberate carve-out for terminal-style
abbreviations (same as `StateMeta.code`). Then run `scripts/translate-ui.sh`.

**Tests** — `frontend/src/__tests__/PromptInjector.test.tsx`: trigger renders only on `queued`
rows; trigger click does not select the row; open → edit → save calls the API and lights the
trigger; all-blank save sends the clearing shape; CLEAR does not close; preset paste
overwrites all three fields; backdrop click discards. Plus a clamp unit test asserting
`y + panelH <= vh - 12` for a click near the bottom edge.

---

## Phase 5 — "JOB POSTING" link in the review tab row (folded-in scope)

> Unrelated to prompt injection; folded in at the user's request as a small, self-contained
> addition to the same branch.

### Current State
`ReviewPane.tsx:172` renders the tab bar as a bare `display: flex` row with a bottom border,
holding `tabButton("cv", …)` and — in `final` mode only — `tabButton("cl", …)`. The row's
right-hand side is empty. `JobDTO.link` already carries the job-posting URL ingested from the
CSV (`jsa/ingest/csv_loader.py` → `Job.link`, exposed by `_job_to_dict`) and is currently
surfaced nowhere in the review surfaces. `theme/Icon.tsx` already ships a `link` glyph.

### Desired State
A right-aligned "JOB POSTING" button on that same tab row, baseline-aligned with the CV /
COVER LETTER tabs, opening the original posting in a new tab. Present in **both** review
stages — `cv-gate` mode (the CV revise stage, `cv_review`) and `final` mode (the cover-letter
revise stage, `review`) — since both render this one tab bar.

### Problems/Bugs
- `link` can be empty: the CSV column is free text and nothing validates it as a URL. Rendering
  an `<a href="">` would give a control that silently navigates to the app's own root.
- A `<button>` + `window.open()` would lose middle-click, cmd-click and "copy link address".
  The correct element is an `<a>`.
- `target="_blank"` without `rel="noopener noreferrer"` hands the opened page a live
  `window.opener` reference back into the app.

### Solutions
In `ReviewPane.tsx`, read the link alongside the other store selectors:
```ts
const jobLink = useStore((s) => s.jobs[jobId]?.link);
```
and append to the tab-bar row (line 172), rendered only when `jobLink?.trim()` is non-empty:

```tsx
<a
  href={jobLink}
  target="_blank"
  rel="noopener noreferrer"
  title={t("reviewPane.jobPostingTitle")}
  style={{
    marginLeft: "auto",            // flush right, tabs stay left
    alignSelf: "center",
    display: "inline-flex", alignItems: "center", gap: 6,
    padding: "5px 10px", marginBottom: 4,
    border: `1px solid ${T.bd2}`, borderRadius: T.btnRadius,
    background: "transparent", color: T.ink2,
    font: `600 10px ${T.mono}`, letterSpacing: ".06em",
    textDecoration: "none",
  }}
>
  <Icon name="link" size={11} />
  {t("reviewPane.jobPosting")}
</a>
```

`marginLeft: "auto"` is what pushes it fully right without touching the tab buttons' own
`marginRight: 22` spacing. `Icon` is already imported in this file's sibling components; add
the import here if absent.

**i18n**: `reviewPane.jobPosting` = `"JOB POSTING"` and `reviewPane.jobPostingTitle` =
`"Open the original job posting in a new tab"` in `strings.en.json`, then
`scripts/translate-ui.sh`.

**Tests** — extend `frontend/src/__tests__/ReviewPane.test.tsx`: the link renders with the
job's `link` as `href` and `rel="noopener noreferrer"` in both `cv-gate` and `final` mode, and
does **not** render when `link` is empty or whitespace.

---

## Phase 6 — Documentation and verification

### Current State
CLAUDE.md's "Prompt caching (HTTP API backends)" opens with **The cross-job system-prefix
invariant**, stated absolutely: *"no per-job bytes (JD, company, role, CV structure, research
brief) ever land in it"*.

### Desired State
The invariant narrowed to match reality, plus a new section documenting the feature — written
in the same "do not "simplify" this back out" register the rest of the file uses, because
every part of this design looks removable to someone who hasn't read the rationale.

### Solutions

**Amend the cross-job invariant paragraph** to carry the three-row table from the Context
section above: a job with **no** injection produces the byte-identical shared prefix
(unchanged, and gated by `test_prompt_prefix_stability.py`), so uninjected jobs keep reading
each other's warm entry; an injected job running between them does not evict it (entries are
keyed by prefix, not a single slot); intra-job caching across an injected job's own turns is
unaffected (the injection is frozen pre-launch, so that job's prefix is stable turn to turn);
and only the *read into* an injected job's first turn is forfeited. Record the rejected
two-block split there too, so the next agent doesn't re-derive it as a "fix".

**Add a "Per-job prompt injection" section** covering: the `queued`-only gate; the
`prefix + prompt_text + postfix` ordering and *why* the machine sections keep final position;
the single-resolution / all-three-call-sites invariant; the fit-gate carve-out (system yes,
`first_msg` no) and why; and the normalize-to-`None` rule that keeps the shared prefix intact
for blank input.

### Verification

Backend:
```bash
pip install -e .
pytest -v -m "not integration"                       # full suite must stay green
pytest tests/backend/test_prompt_prefix_stability.py tests/backend/test_prompt_assembly.py -v
pytest tests/backend/test_prompt_injection.py tests/backend/test_injection_presets.py -v
```
The two gate files must pass **with no edits to their existing cases** — that is the proof
the invariant was narrowed rather than broken.

Frontend:
```bash
cd frontend && npm install && npm test && npm run build
```
`npm run build` is not optional: the `jsa` CLI serves the gitignored `jsa/static` bundle, not
live source, so a UI change is invisible to the smoke test without it.

End-to-end smoke:
1. `jsa --csv <jobs.csv> --cv <resume.pdf> --no-browser`, open `http://localhost:8765`.
2. On a `queued` row: syringe is present, LAUNCH is to its right; on a `running`/`approved`
   row it is absent.
3. Click the syringe near the **bottom edge** of the window — the footer buttons must be
   fully visible (the clamp regression).
4. Fill all three fields → SAVE INJECTION → the trigger turns accent-colored; reload the page
   → it is still lit (server-persisted, not component state).
5. Save a preset, reopen on a **different** job, PASTE it → all three fields populate.
6. LAUNCH the job. In the transcript / `Message` rows, confirm the system prompt starts with
   the prefix and carries the postfix *before* the structured contract, and that the first
   user message ends with the `ADDITIONAL INSTRUCTIONS FROM THE USER` block. Answer a
   follow-up to force a resume and confirm the wrapper is **still** in the resent system
   prompt (Phase 2, Problem 2).
7. Reopen the syringe on that now-`running` job — the trigger must be gone; `curl -X PUT` its
   injection endpoint directly and confirm a 400.
8. (Phase 5) On a job parked in `cv_review` **and** one in `review`: the JOB POSTING button is
   flush right on the tab row, level with the CV / COVER LETTER tabs, and opens the CSV's
   posting URL in a new tab. On a job whose CSV row has no link, the button is absent.

Optional: Playwright is worth using for steps 2–5 if it is installed on this machine.

Finally, per CLAUDE.md → "Code Review Rules": this is a large, multi-surface change →
`/code-review medium --fix` (Sonnet, never an Opus override) once every todo item is checked.

---

## Branch

Branch `feat/prompt-injection` off `main` before any edit; no commits to `main`; no merge
without explicit approval.

---

## Change Log

_(entries appended as phases land — format: **YYYY-MM-DD**: context, actions, decisions,
verification result)_

---

**2026-09-04** — *Merged into `main`'s integration branch alongside the other two
in-flight features.* Context: `feat/revision-tool-use`, `feat/cv-decks` and
`feat/prompt-injection` were finished in three separate worktrees off the same `main`
and had to be brought together. Actions: created
`chore/integrate-tooluse-decks-injection` off `main` and merged all three `--no-ff`, in
that order — tool-use first (largest, and the only one whose merge base predates main's
current-date directive `e7c116e`), cv-decks second (it touches neither `stages.py` nor
`prompt_assembly.py`, by its own locked decision 1), prompt-injection last, since it is
the only branch that collides with both. Decisions:
- **The load-bearing resolution.** This branch computes `base` (prefix + prompt_text +
  postfix) at the top of `assemble_system_prompt` and rewrites every branch to compose
  from it. `feat/revision-tool-use` adds a NEW FIRST branch returning `prompt_text +
  _tool_contract(...)`. Different regions of one function, so the naive keep-both
  computes `base` and never reads it on the tool path — the per-job wrapper silently
  vanishes from every tool-mode revision. `base` is now computed BEFORE the tool branch,
  which returns `base + _tool_contract(...)`, and the "below this block `prompt_text` is
  never read again" invariant is stated in the code, in CLAUDE.md, and pinned by a test.
- `_tool_system_prompt` in `stages.py` — a fourth `assemble_system_prompt` call site that
  did not exist on this branch — now receives `injection=` too. On the prompt rung that
  system prompt is the model's only transport, so the omission was completely silent.
- `jsa/db/engine.py`: the conflict split each feature's `ALTER TABLE` block before its own
  `except OperationalError`, so keep-both produced a `try` with no handler. Caught here
  only because it happened to be a syntax error; a split one line either way would have
  dropped one migration silently, and nothing in the suite would have noticed — every
  other test builds its schema with `create_all`. There is now a test for the upgrade path.
- `JobList.tsx` queued row: all three controls in one wrapper, in the CV-decks hand-off's
  order (BaseCvTrigger, InjectTrigger, LaunchButton). Kept this branch's
  `<span display:inline-flex>` over cv-decks' `<div>` — the row is a `<button>`, whose
  content model is phrasing.
- **The i18n fan-out this plan requires had never been run** (this file's Change Log was
  empty): 22 keys were missing from all 19 locale catalogs. Run as part of the merge;
  `translate-ui.sh --check` is now clean.
- Deliberate divergence left in place: `PUT /api/jobs/{id}/injection` answers 400 on the
  pre-launch gate while the sibling `/base-cv` route answers 409. Each was specified
  against its own design hand-off and each is pinned by its own tests; noted in the code
  rather than unified unilaterally.

Cross-cutting: every feature branch's suite runs against a tree containing exactly ONE
feature, so none of them can see an interaction and a bad merge stays green in all three
— demonstrated, not assumed (reintroducing the `prompt_text`/`base` bug above left all
2210 pre-merge tests passing). Added `tests/backend/test_feature_integration.py` (8),
`test_prompt_assembly.py::TestToolContractCarriesTheInjection` (6) and two
`JobList.test.tsx` cases as the merge's actual deliverable, each mutation-tested.
Verification — **verified**: backend 2337 passed / 2 skipped, and the collected node set
is an exact superset of the union of all four branches' node sets (2325 before the new
tests), so no test was lost in any merge; frontend 456 passed across 28 files (no test
file dropped); `tsc --noEmit` clean. Not done: merge to `main` (awaiting explicit
approval per the git branch policy) and the manual end-to-end smoke checks.

**2026-09-04**: context — the CLAUDE.md-mandated post-implementation review
(`/code-review medium 3b3b61f^..HEAD`, scope verified as this branch's integration
commits) landed after the merge was committed; triage its five findings. actions —
applied one (`store.saveInjection` now toasts `detailOf(err, …)` on failure, mirroring
the sibling `assignBaseCv`, pinned by two new `store.test.ts` cases); rejected three
(no `max_length` on the injection PUT body — v1 is local-only single-user and the only
poster is the user's own vial; the preset-save revert staying console-only — the toast
channel is job-keyed and presets are global, so surfacing it is a design call, not a
fix; `_THINKING_BUDGET` — `includeThoughts` already created the whole `thinkingConfig`
rejection surface, and the pinned budget is itself the fix for a live-observed starved
reply); escalated one (a failed `hydrateInjectionPresets` leaves `injectionPresets: []`
with no retry, and the next dose save whole-list-PUTs one entry over the server library
— real, but the guard is a UX choice). decisions — the reviewer's "boot race" trigger
for that finding is wrong for the bundled path but right for `npm run dev`'s split
origin and for a tab outliving a `jsa` restart; its corrupt-file trigger is weak, since
overwriting an unparseable file with one valid entry is closer to recovery than loss.
Also recorded here because it was not accounted for at merge time: this branch carried
two commits unrelated to prompt injection that the merge brought onto the integration
branch — `60e3420` (cap Gemini's `thinkingBudget`) and `e3c686b` (the ReviewPane JOB
POSTING link, folded in as Phase 5). verification — **verified**: frontend 458 passed
across 28 files, `tsc --noEmit` clean, `npm run build` clean (bundle rebuilt); backend
untouched by the applied fix. Zero merge defects were found by the review — it
independently confirmed all four `assemble_system_prompt` call sites thread
`injection=`, the `prompt_text`→`base` invariant in every branch, and the 22 i18n keys
across en/ru/ar.

---

## Decisions Log

2026-09-04: Overriden decisions:
- Option C approved then killed on evidence (leak hypothesis refuted by function-scoped event loops)
- The 560 → 80vh clamp override above
- Deferring the single-transaction _handle_session_expired fix
