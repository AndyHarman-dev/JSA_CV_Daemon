---
status: InProgress
---

# Split the pipeline into two sequential lanes (CV lane → Cover-letter lane)

## Context

Today a job runs `fit_assessment → cv_adjust → cover_letter` and only *then* parks in
`review`, where the CV and the cover letter are presented together. The user has to hold both
artifacts in their head at once, and the cover-letter agent has to carry a long chain of
context to reach the end of a single, undifferentiated run.

The goal is two sequential, context-independent lanes sharing only the job description:

1. **CV lane** — `cv_adjust` runs, the user goes back and forth with the agent, and the job
   **parks on the CV alone**. The user either revises it or approves it. On approval the CV is
   rendered and dumped to `output/`, and only then does the job advance.
2. **Cover-letter lane** — company research runs, then the letter is written against the JD +
   **the just-approved tailored CV**. Parks in the existing final `review`.

The user must also be able to navigate back to the CV view from the timeline while the
cover letter is being written — read-only, because the only way to be past the CV lane at all
is to have approved the CV.

### What is already in place (do not rebuild this)

Three of the four stated caveats are already satisfied by the current code — verified:

- **Context-independent tasks.** `cv_adjust` and `cover_letter` already run in *separate*
  agent sessions (`Job.cv_session_id` / `Job.cl_session_id`, `jsa/db/models.py:52-53`), with
  separate prompt files and separate `_load_history(job.id, stage)` replay. There is no shared
  conversation to split.
- **Research is already per-stage and lazy.** `_gather_research` (`jsa/pipeline/stages.py:499`)
  fires only on the *fresh-session* branch of a stage, and `_research_spec`
  (`stages.py:1156`) picks `cl-research` → `[COMPANY_BRIEF]` for `cover_letter` and
  `cv-research` → `[INTEL_BRIEF]` for `cv_adjust`. The **company** brief already runs when
  the cover-letter lane starts, not at job start. What runs at job start is the *CV intel*
  brief — a different artifact serving CV tailoring.
- **CV writing is already an isolated agentic job across providers.** `PROMPT_CDADJUST.md`,
  its own session, backend resolved from `job.backend_name` through the BF-19 fallback chain.

### The actual delta

- **A CV approval gate does not exist.** `running(cv_adjust)` checkpoints straight to
  `cv_done`, and `cv_done` *means* "runnable → cover_letter" (`_next_stage_for`,
  `orchestrator.py:47`; `list_runnable_jobs`, `jsa/db/repo.py:93`).
- **The cover-letter agent never sees the tailored CV.** `_build_initial_user_msg`
  (`stages.py:1013`) hands *both* stages the same raw `BASE CV STRUCTURE` from
  `cv_structure.json`. The letter is written against the base CV, not the one that will
  actually be submitted.
- **The timeline is inert.** `frontend/src/components/StageTimeline.tsx` has no `onClick`
  anywhere, and there is no notion of "stage being viewed" vs "stage running".
- **`cv-research` should be unwired** from `cv_adjust`.

### Decisions locked (from user Q&A)

| Question | Decision |
|---|---|
| Jump-back to CV while CL runs | **Read-only view.** No revise box; you can only be past the CV lane by having approved it. |
| CL agent's CV content | **Tailored CV only** — the approved `cv_adjust` Document replaces the base-structure block. |
| CV approval output | **Render + dump now.** CV PDF+DOCX land in `output/` at the CV gate; final approve does not re-render the CV. |
| `cv-research` | **Unwire, keep files.** Stop calling it for `cv_adjust`; leave `.claude/agents/cv-research.md` and `jsa/prompts/GEMINI_CV_RESEARCH.md` on disk. |
| Revision at final review | **Both CV and CL remain revisable** (today's behaviour). |
| "Not proceeding further with the job queue" | Interpreted as **this job** parks; the orchestrator keeps dispatching other jobs. |

> ⚠️ **Flagged consequence of "both revisable at final review":** revising the CV at final
> review leaves the already-written cover letter stale against the new CV — it was written
> against the previous version. This plan does **not** auto-invalidate the letter (per the
> decision above); the user is expected to revise the letter too if it matters. Noted here so
> it is a known trade-off, not a surprise.

---

## Phase 1 — New `cv_review` state and the state machine

**Current state.** `JobState` (`jsa/db/models.py:13`) has `cv_done`, reached from
`running(cv_adjust)` and immediately runnable → `cover_letter`. `ALLOWED`
(`jsa/pipeline/state_machine.py`) also permits `running(cover_letter) → cv_done` — that edge
exists **specifically for the BF-19 backend switch** ("cover_letter limit hit; rewind to
cv_done").

**Desired state.** A new parked state `cv_review` sits between `running(cv_adjust)` and
`cv_done`. `cv_done` keeps its current meaning verbatim: *CV approved, cover letter pending,
runnable*.

**Problems/Bugs.**
- Repurposing `cv_done` as the user gate would break the quota-switch rewind: a backend switch
  mid-cover-letter would re-park the job asking the user to approve a CV they already
  approved. This is the single sharpest trap in the change.
- `revising_cv` currently always terminates in `review`. It now has **two** possible exits —
  back to `cv_review` (revision requested at the CV gate) or to `review` (revision requested
  at final review) — and `_handle_final` has no way to tell them apart, because the job is
  `running` at that point.
- `set_current_stage` (`state_machine.py:77`) hard-asserts `job.state == review`.

**Solutions.**

1. Add `cv_review = "cv_review"` to `JobState` (`jsa/db/models.py`).

2. `jsa/pipeline/state_machine.py` — edit `ALLOWED`:

```python
JobState.running:   {..., JobState.cv_review, ...},          # add cv_review
JobState.cv_review: {JobState.running,        # revising_cv
                     JobState.cv_done,        # user approved the CV
                     JobState.failed, JobState.dismissed},
JobState.awaiting_input: {..., JobState.cv_review, ...},     # a CV-lane NEED_INPUT that finalises
JobState.cv_done:   {JobState.running, JobState.failed, JobState.dismissed},   # UNCHANGED
```

`STAGE_FOR_STATE[JobState.running]` already contains `cv_adjust`/`revising_cv`; add nothing.
`cv_review` is a parked state → `current_stage` must be `None`, **except** while a
`revising_cv` request is pending (mirroring how `review` carries `revising_*`). Extend
`set_current_stage` to accept `job.state in (JobState.review, JobState.cv_review)`, and when
state is `cv_review` accept **only** `Stage.revising_cv`.

3. Add the guard, mirroring the existing `running → cv_done` one: `running → cv_review` is
   valid only from `current_stage in (cv_adjust, revising_cv)`.

4. **Leave `running(cover_letter) → cv_done` exactly as it is.** Add a comment naming it as
   the BF-19 rewind target so a future reader does not "helpfully" redirect it to `cv_review`.

5. **Two exits for `revising_cv`** — add a nullable column to `RevisionRequest`
   (`jsa/db/models.py:120`):

```python
origin_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
# JobState value the revision was requested from: "cv_review" | "review".
# NULL (legacy rows) is read as "review".
```

`jsa/db/engine.py`'s migration story is `create_all` + additive `ALTER TABLE`, which this
fits. `_handle_final` for `revising_cv` reads `rev_req.origin_state` and checkpoints to
`cv_review` or `review` accordingly. `revising_cl` is unaffected (always `review`).

**Files:** `jsa/db/models.py`, `jsa/pipeline/state_machine.py`, `jsa/db/engine.py`.

---

## Phase 2 — Orchestrator and runnable-job selection

**Current state.** `_next_stage_for` (`orchestrator.py:33`) maps `cv_done → cover_letter` and
`awaiting_input`/`review → job.current_stage`. `list_runnable_jobs` (`jsa/db/repo.py:93`)
picks up `pending`, `fit_done`, `cv_done`, answered-`awaiting_input`, and `review` +
unconsumed revision.

**Desired state.** A `cv_review` job is **not** runnable — it is parked — *unless* it carries
an unconsumed `RevisionRequest`, exactly as `review` behaves today.

**Problems/Bugs.** Without the `list_runnable_jobs` clause, a CV revision requested from the
gate would be inserted and then never dispatched. Without the `_next_stage_for` clause, the
dispatch would raise `ValueError: unexpected state`.

**Solutions.**

- `list_runnable_jobs`: reuse the already-defined `unconsumed_revision` EXISTS subquery and add
  one `or_` arm: `(Job.state == JobState.cv_review) & unconsumed_revision`. Nothing else — the
  bare `cv_review` state must stay unrunnable.
- `_next_stage_for`: extend the `awaiting_input`/`review` branch to
  `job.state in (JobState.awaiting_input, JobState.review, JobState.cv_review)` → return
  `job.current_stage`. Update the docstring's state table.
- `cv_done → cover_letter` stays as-is.

**Files:** `jsa/pipeline/orchestrator.py`, `jsa/db/repo.py`.

---

## Phase 3 — Stage behaviour: CV gate, tailored-CV handoff, research unwiring

**Current state.** `_handle_final` (`stages.py:846`) maps `cv_adjust → cv_done`,
`cover_letter → review` (+ `_render_for_review`), `revising_* → review` (+
`_render_for_review`). `_render_for_review` (`stages.py:74`) renders **both** the CV and the
cover letter. `_build_initial_user_msg` (`stages.py:1013`) injects `brief` + `BASE CV
STRUCTURE` + JD + tier for both primary stages. `_gather_research` runs for both.

**Desired state.**
- `cv_adjust` finalising → `cv_review`, with the CV rendered to PDF+DOCX in `output/`.
- `cover_letter`'s initial message carries the **approved tailored CV**, not the base structure.
- Research runs for `cover_letter` only.

**Problems/Bugs.**
- `_render_for_review` renders both artifacts; at the CV gate no `cover_letter` Document
  exists yet. It must be able to render a single stage without erroring or emitting a
  half-written letter.
- The cover-letter agent writing against the base structure is precisely the context gap the
  two-lane split is supposed to close.
- `_research_spec` will be reached with `cv_adjust` and must no longer produce a `cv-research`
  spec.

**Solutions.**

1. **CV gate.** In `_handle_final`, `cv_adjust` → `checkpoint(..., JobState.cv_review, None,
   document=...)`, then `await _render_cv(session, job, output_dir)`. For `revising_cv`,
   resolve the destination from `rev_req.origin_state` (Phase 1) and render accordingly.

2. **Render.** Refactor `_render_for_review(session, job, output_dir)` into
   `_render(session, job, output_dir, stages=(Stage.cv_adjust, Stage.cover_letter))` — same
   body, iterating the requested stages and skipping any with no Document. Keep
   `_render_for_review` as a thin wrapper (both stages) so the existing two call sites at
   `stages.py:928,949` and their tests are untouched; add `_render_cv` = `_render(...,
   stages=(Stage.cv_adjust,))`. This satisfies "render + dump now": the CV PDF/DOCX land in
   `output_dir` at the gate, and `approve` (which renders nothing today) stays as it is.

3. **Tailored-CV handoff.** Give `_build_initial_user_msg` an explicit CV-content argument
   instead of always reading the base structure. In `run_stage`'s fresh-session branch:

```python
if stage is Stage.cover_letter:
    cv_docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
    # version-desc; the CV lane cannot be past the gate without one
    cv_block = ("TAILORED CV (approved by the user — write the letter against this)",
                cv_docs[0].markdown)
else:
    cv_block = ("BASE CV STRUCTURE", await _read_base_structure(cv_structure_path))
```

   Use `Document.markdown` (the canonical serialisation) rather than `structured` — the
   letter-writer wants prose, not a JSON skeleton. If no `cv_adjust` Document exists (should
   be unreachable), fall back to the base structure and log a warning rather than failing the
   job. Update `CVL_PROMPT.md` **STEP 2** so it refers to the tailored CV block instead of
   `BASE CV STRUCTURE`, and add a line forbidding the agent from asserting anything not in it.

4. **Unwire `cv-research`.** In `run_stage`'s fresh branch, call `_gather_research` only when
   `stage is Stage.cover_letter`; `cv_adjust` gets no brief block at all (not a `NONE`
   placeholder — omit the block, and have `_build_initial_user_msg` accept `brief=None`).
   `_research_spec` keeps only the `cover_letter` arm and raises on anything else. **Do not
   delete** `.claude/agents/cv-research.md` or `jsa/prompts/GEMINI_CV_RESEARCH.md`; do remove
   the `cv_adjust` entry from `google_cli.py`'s prompt map only if it is dead weight, and
   strip the `[INTEL_BRIEF]` handling instructions from `PROMPT_CDADJUST.md`.

**Files:** `jsa/pipeline/stages.py`, `jsa/prompts/CVL_PROMPT.md`,
`jsa/prompts/PROMPT_CDADJUST.md`.

---

## Phase 4 — API surface

**Current state.** `approve_job` (`routes_jobs.py:198`) requires `state == review` **and**
both a `cv_adjust` and a `cover_letter` Document. `revise_job` (`:251`) requires
`state == review`. `export_job` (`:663`) requires `state in (review, approved)`.
`GET /api/jobs/{id}/document/{stage}` (`:621`) already exists and is what the frontend needs
for stage-jumping — do not add a new read endpoint.

**Desired state.** A CV-only approval endpoint, and revise/export/dismiss/cancel that
understand `cv_review`.

**Problems/Bugs.** Overloading `approve` would be wrong — its cover-letter-Document
precondition is meaningful for the final approval and must not be relaxed.

**Solutions.**

- **`POST /api/jobs/{id}/approve-cv`** — requires `state == cv_review`; requires a `cv_adjust`
  Document; `await repo.checkpoint(session, job, JobState.cv_done, new_stage=None)`; publish a
  `StatusChangedEvent`; `request.app.state.orchestrator.kick()` so the cover-letter lane starts
  immediately. Returns the CV's `pdf_path`/`docx_path`.
  **Orphaned-revision guard:** `cv_review` + an unconsumed `RevisionRequest` is a legitimate
  parked combination (it is the Phase 2 `list_runnable_jobs` arm). If approve-cv fired in that
  window the job would move to `cv_done`, whose runnable arm has no revision condition, and the
  request would never be consumed or dispatched. So `approve-cv` **rejects with 400 when an
  unconsumed `RevisionRequest` exists** for the job.
- **`revise_job`** — accept `state in (review, cv_review)`. When `cv_review`, reject
  `target == "cl"` with a 400 (no letter exists yet). Stamp `RevisionRequest.origin_state =
  job.state.value` before `set_current_stage`.
- **`reset_job` / `soft_reset_job` need no change — verified.** `soft_reset_job`
  (`jsa/db/repo.py:192`) takes the `failed → cv_done` edge **only** when
  `job.current_stage in (cover_letter, revising_cl)` — i.e. only when the CV was already
  approved, so it cannot skip the new gate. A failure in `cv_adjust`/`revising_cv` still
  rewinds to `pending` with a full wipe, which stays correct at the CV gate (nothing was
  approved yet). Do not "helpfully" retarget either branch at `cv_review`.
- **`export_job`** — accept `cv_review` and render only the stages that have Documents.
- **`dismiss_job` / `cancel_job` / `reset_job`** — audit their state guards and admit
  `cv_review` wherever `review` is admitted.
- `_job_to_dict` needs no change (`state` is already serialised from the enum).

**Files:** `jsa/api/routes_jobs.py`, `jsa/api/events.py` (only if a new event type is wanted —
prefer reusing `StatusChangedEvent`).

---

## Phase 5 — Frontend: clickable timeline and the CV gate pane

**Current state.** `StageTimeline.tsx` renders seven non-interactive dots
(`pending, cv_adjust, cv_done, cover_letter, cl_done, review, approved`) via
`getActiveStepIndex(state, currentStage)`. `ReviewPane.tsx` mounts only for
`review`/`approved` (`JobDetail.tsx:434`) and owns a local `activeTab: "cv"|"cl"`. There is no
"viewed stage" concept anywhere. Transport is a WebSocket that mostly triggers
`refetchAll()`.

**Desired state.** The CV and cover-letter nodes are clickable and select which artifact the
detail pane shows, independently of what the pipeline is running. A `cv_review` job shows a
CV-only approve/revise pane.

**Problems/Bugs.** `job.current_stage` is server-driven and single-valued; it cannot express
"user is reading the CV while the letter is being written". Reusing `ReviewPane` wholesale for
the gate would expose an APPROVE-&-EXPORT button and a CL tab that mean the wrong thing there.

**Solutions.**

1. **`types.ts`** — add `"cv_review"` to the `JobState` union.

2. **Store (`src/store.ts`)** — add UI-only `viewedStage: "cv" | "cl" | null` plus
   `setViewedStage`. `null` = follow the pipeline (today's behaviour). Reset it to `null` in
   `selectJob` so switching jobs does not carry a stale view.

3. **`StageTimeline.tsx`** — replace the `cv_done` step with a `cv_review` step (label
   `CV_REVIEW`) and keep `cl_done` → `review` → `approved` as-is. Extend
   `getActiveStepIndex` with `case "cv_review": return 2;`. Make **only** the `cv_adjust` and
   `cover_letter` dots interactive: render them as `<button>` with `onClick={() =>
   setViewedStage("cv" | "cl")}`, `aria-pressed`, and a visible selected affordance;
   every other step keeps rendering as a plain `<div>`. Disable the `cover_letter` button
   while no cover-letter Document exists yet (a `cv_review` job).

4. **`ReviewPane.tsx`** — add a `mode?: "cv-gate" | "final"` prop (default `"final"`, so the
   existing call site and `ReviewPane.test.tsx` are unaffected). In `"cv-gate"`: render the CV
   tab only, swap the approve button to `api.approveCv(jobId)` with a distinct label, and
   restrict the `ChatBox` revise target to `cv`. Drive `activeTab` from the store's
   `viewedStage` when it is non-null, falling back to today's local state otherwise.

   **Read-only is a function of `job.state`, never of `viewedStage`.** `viewedStage` chooses
   *which artifact is shown*; it must never decide whether controls exist. The pane is
   read-only iff the job is in flight (`state === "running" || state === "awaiting_input"`),
   and fully interactive whenever the job is parked awaiting the user (`cv_review` → CV
   approve + CV revise; `review` → APPROVE & EXPORT + revise CV *or* CL, per the locked
   "both revisable at final review" decision). Getting this backwards would strip the
   APPROVE & EXPORT button off the final review pane whenever the user was looking at the CV
   tab — the pane must keep behaving exactly as it does today at `state === "review"`.

5. **`JobDetail.tsx`** — mount `<ReviewPane mode="cv-gate">` when `job.state === "cv_review"`,
   and mount the pane in read-only form (per the rule above) while the job is
   `running`/`awaiting_input` on the cover-letter lane, so the CV stays reachable mid-run.

6. **`api.ts`** — add `approveCv(jobId)` → `POST /api/jobs/{id}/approve-cv`.

7. **i18n** — every new string goes into `src/i18n/strings.en.json` under stable dotted keys
   (`stageTimeline.cvReview`, `reviewPane.approveCvButton`, `reviewPane.readOnlyNotice`,
   `reviewPane.clNotStarted`, …) rendered through `useT()`. Then run
   `scripts/translate-ui.sh` (incremental) and verify with `scripts/translate-ui.sh --check`.

8. **`npm run build`** — the `jsa` CLI serves the gitignored `jsa/static` bundle, not live
   source. Skipping this makes the change invisible at runtime.

**Files:** `frontend/src/types.ts`, `store.ts`, `api.ts`,
`components/{StageTimeline,ReviewPane,JobDetail}.tsx`, `i18n/strings.en.json`.

---

## Phase 6 — Update `CLAUDE.md`

**Current state.** The project `CLAUDE.md` documents conventions this change falsifies:

- The **"Renderer invocation"** section states that renderers run on review entry and that
  "`POST /api/jobs/{id}/approve` does **no** rendering… Do not move rendering back onto
  `approve`." The CV gate adds a *third* render trigger (CV-only, on `cv_review` entry).
- The **"Fit-assessment gate"** section describes `fit_done` being treated "exactly like
  `cv_done` in `list_runnable_jobs` and `_next_stage_for`" — still true, but the surrounding
  state flow now has `cv_review` in it.

**Desired state.** A future agent reading `CLAUDE.md` recognises the CV-gate render as
intended rather than as a bug to revert.

**Solutions.** Extend the renderer section to name all three triggers (CV-only on `cv_review`
entry; both artifacts on `review` entry and on every revision completion; `approve` still
renders nothing). Add a short **"Two-lane pipeline / CV gate"** section covering: `cv_review`
as a parked state, `cv_done` still meaning *CV approved, CL pending* and still being the BF-19
rewind target, `RevisionRequest.origin_state` deciding where `revising_cv` returns, and the
cover letter being written against the approved `cv_adjust` Document rather than
`cv_structure.json`. Note that `cv-research` is unwired but its files are retained
deliberately.

**Note:** `ARCH.md` is referenced by `CLAUDE.md` but does not exist in the repo — do not try
to update it, and do not create it as part of this change.

**Files:** `CLAUDE.md`.

---

## Phase 7 — Tests

**Current state.** Backend: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`), fakes over
mocks (`tests/backend/fakes/fake_backend.py`). Frontend: `vitest` +
`@testing-library/react`. Per memory, ~27 backend and ~9 frontend tests already fail on HEAD
from pre-existing debt — **baseline the suite before starting** so new failures are
distinguishable.

**Desired state.** The new gate, the handoff, and the timeline are covered; nothing that
passed before regresses.

**Solutions.**

- `test_state_machine.py` — `running(cv_adjust) → cv_review` allowed; `cv_review → cv_done`
  allowed; `cv_review → review` rejected; `running(cover_letter) → cv_done` **still**
  allowed (the BF-19 rewind — assert it explicitly, it is the trap); `set_current_stage` on
  `cv_review` accepts `revising_cv` and rejects `revising_cl`.
- `test_orchestrator.py` — a bare `cv_review` job is not dispatched; a `cv_review` job with an
  unconsumed `RevisionRequest` dispatches `revising_cv`; `cv_done` still dispatches
  `cover_letter`.
- `test_stages.py` — `cv_adjust` FINAL lands in `cv_review` and a CV PDF+DOCX exist in
  `output_dir` while no CL file does; the `cover_letter` initial user message contains the
  `cv_adjust` Document's markdown and **not** the base-structure JSON; no research call is made
  for `cv_adjust`; `revising_cv` with `origin_state="cv_review"` returns to `cv_review` and
  with `"review"` returns to `review`.
- `test_routes_jobs.py` — `approve-cv` happy path + wrong-state 400; `approve-cv` with an
  unconsumed `RevisionRequest` → 400 (the orphaned-revision guard); `revise` from `cv_review`
  with `target="cl"` → 400; `approve` from `cv_review` → 400.
- `test_repo.py` / reset — assert `soft_reset_job` on a job failed in `cv_adjust` still lands
  in `pending`, and on one failed in `cover_letter` still lands in `cv_done` (neither routes
  through `cv_review`).
- Frontend — `StageTimeline.test.tsx`: CV/CL dots are buttons, the other five are not, clicking
  sets `viewedStage`, CL is disabled with no CL document. `ReviewPane.test.tsx`: `cv-gate`
  mode shows one tab and the CV-approve button; a `running(cover_letter)` job with
  `viewedStage === "cv"` hides the revise box; and — the regression that matters — a
  `state === "review"` job with `viewedStage === "cv"` **still** shows APPROVE & EXPORT and
  the revise box.

---

## Verification

```bash
# 0. Baseline BEFORE any edit — record which tests already fail on HEAD
pytest -m "not integration" -q | tail -5
cd frontend && npm test 2>&1 | tail -5; cd ..

# 1. Backend
pip install -e .
pytest -m "not integration" -v

# 2. Frontend
cd frontend && npm test && npm run build && cd ..
scripts/translate-ui.sh --check

# 3. End-to-end smoke (fresh DB)
#    /reset-test-db, then the standard run command, then in the browser:
#      a. Launch a job → fit gate passes → CV is written → job parks in CV_REVIEW
#      b. Confirm output/ already contains the CV .pdf and .docx, and NO cover letter
#      c. Revise the CV once → returns to CV_REVIEW, files re-rendered
#      d. Approve the CV → cover-letter lane starts (company research, then the letter)
#      e. WHILE it runs, click the CV_ADJUST dot → CV preview, read-only, no revise box
#      f. Click COVER_LETTER dot → back to the running lane
#      g. Letter finishes → final review → approve → both artifacts in output/
```

Playwright is worth using for steps (a)–(g) if it is installed — the clickable-timeline
behaviour is exactly the kind of thing a screenshot diff catches and a unit test does not.

---

## Change Log

_(entries added as phases land)_

**2026-08-28**: Phase 5 (frontend: clickable timeline + CV gate pane). Phases 1–4 had
already landed (commit `8139167`, incl. their Phase-7 backend tests). Implemented:
`types.ts` (`cv_review` added to `JobState`); `theme/chrome.tsx` (`StateMeta` needs a
`cv_review` entry for every `JobState` — required for `tsc`, not called out explicitly in
the plan's file list); `JobList.tsx`/`Header.tsx` (`cv_review` folded into the existing
`review` bucket/count — a bare-state job wasn't in *any* GROUPS array before this, so it
would've vanished from the sidebar entirely, another gap the plan's file list didn't
name); `store.ts` (`viewedStage` + `setViewedStage`, reset on `selectJob`); `api.ts`
(`approveCv`); `ChatBox.tsx` (`fixedTarget` prop, restricts revise to `cv` without a
selector); `StageTimeline.tsx` (relabelled step 2 `cv_review`, `cv_adjust`/`cover_letter`
dots are real `<button>`s wired to `viewedStage`, `cover_letter` disabled until a CL
Document can exist — proxied off `job.state`/`current_stage` since `StageTimeline` only
gets a `JobDTO`, not documents); `ReviewPane.tsx` (`mode="cv-gate"|"final"`, `activeTab`
reads `viewedStage` first then falls back to local state — tab buttons now write to the
store too, so StageTimeline and ReviewPane's own tabs stay in sync — `readOnly` is a pure
function of `job.state` per the plan's explicit warning, never of `viewedStage` or
`mode`); `JobDetail.tsx` (mounts `ReviewPane mode="cv-gate"` for `cv_review`, and again
read-only when `running`/`awaiting_input` on the cover-letter lane with
`viewedStage==="cv"`, else falls through to the existing `FollowUpPane`/nothing).
i18n: added `stateMeta.cvReview`, `stageTimeline.cvReview`, `stageTimeline.clNotStarted`,
`reviewPane.approveCvButton`, `reviewPane.readOnlyNotice` to `strings.en.json`, ran
`scripts/translate-ui.sh --backend cli` (no `ANTHROPIC_API_KEY` in this environment) for
all 18 locales, verified with `--check`. Updated `StageTimeline.test.tsx` (label rename,
button-vs-div split, disabled-until-CL-doc, click-toggles-viewedStage) and
`ReviewPane.test.tsx` (cv-gate tab/approve/revise-target, read-only-while-running notice,
and the plan's named regression: `mode="final"` + `viewedStage="cv"` at `state==="review"`
still shows APPROVE & EXPORT and the revise box). `npm test`: 268/268 passed. `npm run
build` + `tsc`: clean, `jsa/static` regenerated. Did **not** touch Phase 6 (CLAUDE.md doc
update) or the plan's Phase-5 step 3 end-to-end browser smoke — the user is doing the
manual click-through. Verification: unverified pending the user's visual check (Phase 5's
stated gate).

Pre-handoff `advisor()` pass caught three bugs the unit tests didn't reach (all fixed
before handoff, tests green again at 272/272, `tsc`/`npm run build` clean):
1. The COVER_LETTER dot was disabled via a "CL Document exists" proxy keyed off
   `job.state`/`revising_cl`, which stayed `false` for the entire *first* cover-letter run
   (`running`/`awaiting_input`, before its first FINAL) — verification step (f) ("click
   COVER_LETTER dot → back to the running lane") was literally unreachable. Fixed by
   gating on `activeIdx >= 3` instead (`StageTimeline.tsx`), which is true for exactly the
   states where the CL node is reachable, including its first live run.
2. `JobDetail.tsx`'s CV jump-back suppressed `FollowUpPane` whenever
   `awaiting_input` + `viewedStage==="cv"` on the cover-letter lane — if the agent asked a
   blocking question while the user was looking at the CV, the question and its answer box
   both vanished with the pipeline silently stuck. Fixed by restricting the CV-gate
   override to `state==="running"` only; `awaiting_input` now always shows `FollowUpPane`
   unconditionally, matching the plan's own framing of read-only access as "reachable",
   not "exclusive".
3. The CV_ADJUST dot was enabled in every state, but nothing rendered for it during
   `pending`/`queued`/`fit_done`/`running(cv_adjust)`/`running(revising_cv)` — a dead
   click during the CV lane's own live run. Fixed by broadening the same `state==="running"`
   override to apply regardless of which stage is running, not just the cover-letter lane;
   `ReviewPane` already degrades to "Preview rendering in progress…" when no Document
   exists yet, so this is safe with nothing to show.

---

## Decisions Log

_(reserved for the user — not written by the agent)_
