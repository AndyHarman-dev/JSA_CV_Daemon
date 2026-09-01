---
status: Pending
---

# Model-level fallback ladder + rate-limit containment

Branch to create from `feat/multi-backend-model-select`: **`feat/model-fallback-ladder`**

## Context

Four TIER-C jobs were dragged to LAUNCH (EPAM, Innowise, Ubisoft, NDA). Only NDA finished.
The other three each asked a `NEED_INPUT` question, then eventually timed out and failed,
across two backends (`opencode-zen` first, which degraded fast under NVIDIA free-tier load,
then `opencode-go` with various hand-picked models).

**On a rerun with the same backends and the same models, Ubisoft succeeded.** That is the
load-bearing observation: a model that is genuinely down does not start working on a retry.
The dominant failure is **account-scoped rate limiting under 5-way parallelism**
(`Orchestrator.sem = asyncio.Semaphore(5)`, `jsa/pipeline/orchestrator.py:112`) — four jobs
launched together hammer one provider account simultaneously, and the 429/overload is keyed
to the account, not to the model.

Two distinct gaps follow, and the plan treats them in that order of importance:

1. **Rate-limit containment (the demonstrated root cause).** Nothing in JSA limits how many
   concurrent jobs hit *one provider*. Five jobs on `opencode-go` are five simultaneous
   requests on one API key. Raising the timeout *alone* makes this strictly worse — a stuck
   job holds its semaphore slot longer, so throughput under throttling drops.
2. **Model fallback is missing (the real secondary gap).** BF-19 escalates
   `AgentTimeout` / `AgentBackendUnavailable` straight to the **next backend**. With
   `--backends opencode-zen,opencode-go` — two backends, both OpenCode — the chain is two hops
   deep and then the job is dead, even though each backend has a catalog of other models that
   were never tried. When a specific model *is* saturated or down, no timeout value ever
   rescues the job; only a different model does.

Intended outcome: a job survives a busy model by climbing to the next model on the same
backend (cost-ascending, hop-capped), only escalating to the next backend once that ladder is
spent — while the provider sees a throttled, staggered request pattern instead of a burst of
five.

This work is **purely additive** — two new nullable/defaulted columns, one new module, a new
branch *before* the existing backend advance, and new settings. Nothing is replaced,
rewritten, or deleted, so **CLAUDE.md's replace/delete parity gate does not apply**. Note the
exemption rests on "nothing is deleted", *not* on "nothing changes": `max_parallel_per_backend`
is on by default and the 180→300s bump changes every existing HTTP dispatch.

**This plan survived an adversarial skeptic pass**; seven findings were accepted and folded in,
each marked **[skeptic]** below. Four citations were independently re-verified before
acceptance (the internal commit, the `_PROTOCOL` size, the reset paths, the arity counter).

### Locked decisions (from Q&A)

| Question | Decision |
|---|---|
| Throttling approach | **Per-backend concurrency cap** (default 2 in flight per backend) **+ jittered dispatch stagger**. Explicitly NOT lowering global `max_parallel` (5 stays), explicitly NOT adding `Retry-After` 429 handling. |
| Timeout bump | **180s → 300s** for the five HTTP API backends (zen, go, mistral, openrouter, gemini). `anthropic_timeout` and the 600s CLI `agent_timeout` are untouched. |
| Cost guard | **No cost cap** — no multiplier, no absolute ceiling. Ordering stays cost-ascending. |
| Hop-count cap | **5 model hops per job**, tracked in a new `Job.model_hops` column. A *count* cap, not a cost cap — added after the skeptic showed `opencode-go` has 23 rungs, not the handful assumed when the cost decision was locked. |
| Which failures take the ladder | **`AgentTimeout` + `AgentBackendUnavailable` only.** `AgentLimitReached` (429/quota) skips the ladder and advances the backend immediately, as today. |
| opencode-go cost data | **Fetched from OpenCode's docs during implementation** and hand-authored into the static table. Build-time only — never a network call on the failure path. |
| Ladder entry point | The **UI-selected model** (`settings.backend_models[backend]`, else the backend's flat default). Starting every job at the global cheapest would silently override the header dropdown. |

**Consequence of an ascending-only ladder, stated plainly:** if the model you picked in the
header dropdown is already the most expensive rung in that backend's catalog, the job gets
**no ladder at all** — straight to the backend advance. Likewise `openrouter`, whose
`DEFAULT_CATALOG` entry is a single model: no rung to climb to until that catalog grows.

### Design decisions worth stating up front

- **The ladder hop is a job-level rewind, not an in-backend retry.** It reuses
  `repo.backend_switch_reset` with the *same* backend name (verified legal: `ALLOWED[running]`
  covers `pending`/`cv_done`/`review` in `state_machine.py:21`, and `job.backend_name =
  new_backend_name` is simply a no-op assignment). Retrying a different model inside
  `OpenAICompatBackend._call_api` would bypass the stage-Message cleanup and the
  structured/sentinel history adaptation — and the `opencode-go` ladder **crosses the
  `chat`/`messages` protocol boundary** (`_PROTOCOL`: 15 chat + 8 messages), where
  `supports_structured_output` differs per model. The rewind clears `cv_session_id`/
  `cl_session_id`, forcing the fresh-session branch, which is exactly what makes a
  protocol-crossing hop safe.
- **The ladder reads the curated catalog, never the live listing.** `merged_catalog` (code
  defaults + user overrides), not `list_models` — a failure path must not depend on a network
  call to the provider that is already failing.
- **No cost scraping at runtime.** Costs come from a static table, plus OpenRouter's pricing
  which is already in the `/v1/models` payload the code fetches today (confirmed by probe:
  419 models, each with `pricing.prompt`/`pricing.completion`; `:free` variants are `"0"`).
- **A ladder hop costs the user a re-asked question, so it is rationed twice** — by the
  answered-input brake and by the hop cap. See Phase 4.
- **Why the ladder does not contradict CLAUDE.md's "no blind retry of an expensive
  multi-minute call".** That directive keeps `AgentTimeout` out of the *in-backend* retry loop,
  where a repeat is the identical request to the identical upstream. A ladder hop is a
  *different model*, usually a different upstream. This gets stated in CLAUDE.md so a later
  reader does not "simplify" the ladder away. It is also why `AgentLimitReached` is excluded:
  a different model on the *same account* is **not** a different constraint, so hopping on a
  429 would just add requests to an already-throttled key.

---

## Phase 1 — Rate-limit containment (per-backend cap, dispatch stagger, timeout bump)

**Current State.** `Orchestrator.run()` (`orchestrator.py:146-274`) walks
`repo.list_runnable_jobs`, acquires the single global `self.sem` (5), transitions the job to
`running`, and immediately spawns `_run_one`. Nothing knows which backend those five jobs are
on — all five can be one provider account. The five HTTP backends each default to a 180s
per-reply timeout (`jsa/config.py:22-31`).

**Desired State.** At most N jobs (default 2) in flight against any single backend name,
independent of the global 5; consecutive dispatches spread over a small random delay; a
busy-but-alive model gets 300s.

**Problems/Bugs.**
- Four simultaneous launches = four concurrent requests on one API key → provider-side
  throttling that JSA reads as timeouts and hard-fails. This is the reproduced bug.
- A naive fix (blocking on a per-backend semaphore inside the dispatch loop) introduces
  **head-of-line blocking**: the loop is sequential, so a saturated backend would stall
  dispatch for every job behind it, including jobs on completely idle backends.

**Solutions.**
- `jsa/config.py`: add `max_parallel_per_backend: int = 2`
  (`JSA_MAX_PARALLEL_PER_BACKEND`; `0` = unlimited) and
  `dispatch_stagger_seconds: float = 1.5` (`JSA_DISPATCH_STAGGER_SECONDS`; `0` = off).
  Bump `opencode_zen_timeout`, `opencode_go_timeout`, `mistral_timeout`,
  `openrouter_timeout`, `gemini_timeout` from `180.0` to `300.0`.
- `Orchestrator.__init__`: `max_parallel_per_backend` param; lazily-built
  `self._backend_sems: dict[str, asyncio.Semaphore]`.
- In `run()`, **non-blocking acquire** — the head-of-line fix. Resolve `backend_name =
  job.backend_name or self._backends[0]` and attempt an immediate, non-waiting acquisition; if
  the backend is saturated, `continue` (skip this job this cycle, leave it runnable).
- **Release the per-backend slot at every site that releases the global one — all four.**
  `_run_one`'s `finally` (`:389-391`) plus `run()`'s three post-acquire bailouts, each of
  which currently does `self.sem.release(); continue`: `db_job is None` (`:209`),
  `db_job.state == running` (`:214`), and the transition `except Exception` (`:233`). Missing
  any one permanently burns a slot and deadlocks that backend after
  `max_parallel_per_backend` occurrences. Capture which semaphore was acquired at dispatch
  time so a mid-run BF-19 backend switch releases the one actually taken.
- **Stagger inside `_run_one`**, not in the dispatch loop: `await
  asyncio.sleep(random.uniform(0, stagger))` before running the stage. In the loop it would
  delay dispatch itself while holding a slot.
- `jsa/server.py:195-209`: thread both settings into the `Orchestrator(...)` construction.

**Verified invariant that nothing currently pins [skeptic].** Skip-on-saturation is starvation-free
*only because* `self.wakeup.clear()` sits at `orchestrator.py:164`, above the dispatch scan,
with no `await` between `wait()` returning (`:274`) and the next `clear()` — so a kick landing
during the scan is retained. **If `clear()` ever moves below the scan, skipped jobs starve
silently.** Add a comment at `:164` saying so, and a test that a skipped job is picked up after
an unrelated completion.

**Tests** (`tests/backend/test_orchestrator_throttling.py`, new): third job on a saturated
backend is skipped and not transitioned to `running`; a job on a *different* backend in the
same cycle still dispatches (no head-of-line blocking); a skipped job runs after a completion
kick; each of the three bailout paths releases the per-backend slot (drive each, then assert
the backend still dispatches `max_parallel_per_backend` jobs); `max_parallel_per_backend=0`
restores today's behavior.

---

## Phase 2 — Cost-ordered model ladder data

**Current State.** `jsa/agents/model_catalog.py` has `DEFAULT_CATALOG`, `merged_catalog`,
`SUPPORTS_MODEL_SELECTION`, and `list_models` with a 5-min TTL cache. **No cost information
anywhere.** Catalog order is hand-authored, not price-sorted.

**Desired State.** For any backend, a deterministic **cost-ascending** ordering of its curated
catalog, resolvable with zero network I/O.

**Problems/Bugs.**
- Without costs, "try the next model" could jump to the priciest rung — the exact surprise
  called out in the request.
- **[skeptic] With no cost data for `opencode-go`, "unknown sorts last" makes all 23 entries
  tie, and the stable sort silently degrades the ladder to `sorted(_PROTOCOL)` — alphabetical**
  (`glm-5.3 → hy3 → kimi-k2.6 …`). That fails the core requirement precisely on the backend
  these jobs actually ran on.
- Scraping provider doc pages at hop time would add a network dependency to the failure path.

**Solutions.** New module `jsa/agents/model_costs.py`:
- `STATIC_COSTS: dict[str, dict[str, float]]` — backend → model ID → USD per 1M output tokens,
  collected **offline during implementation**. First implementation step for this phase:
  fetch OpenCode's pricing docs and hand-author entries for all **23** `_PROTOCOL` models; do
  the same for Mistral/Gemini/Anthropic. Every `-free` / `:free` model is `0.0`.
- `cost_for(backend, model) -> float | None`, `cost_ordered(backend, models) -> list[str]`
  (ascending; **stable**, so ties keep catalog order; unknown-cost sorts last), and
  `next_model(backend, current, models) -> str | None`.
- **OpenRouter live pricing, opportunistically.** `_fetch_openrouter`
  (`model_catalog.py:159-163`) already downloads pricing and discards it. Extend it to populate
  a module-level `{model_id: cost}` cache that `cost_for("openrouter", …)` prefers. No new
  network call, on any path.
- **Honesty about where the cost machinery actually bites** — document in the module
  docstring: `opencode-zen`'s 5 catalog entries are all free (all tie → catalog order, cost
  sorting is a no-op there); `openrouter` ships **1** catalog entry, so despite having the best
  price data it cannot hop until that catalog grows; `opencode-go` is the backend where
  ordering genuinely matters, which is why its table is authored by hand rather than left to
  the unknown-cost fallback.

**Tests** (`tests/backend/test_model_costs.py`, new): ascending order; stable tie-break for the
all-free zen catalog; unknown-cost sorts last; `next_model` returns the next rung and `None` at
the top; OpenRouter live pricing overrides the static entry when cached; **and a guard test that
every `_PROTOCOL` model has a `STATIC_COSTS` entry**, so a future model added to `opencode_go.py`
fails loudly instead of silently re-alphabetising the ladder.

**Verification item for the user** (no provider keys are exported in my shell) — if OpenCode
exposes cost in its listing, the go table can be derived instead of hand-authored:
`! curl -s -H "Authorization: Bearer $OPENCODE_API_KEY" https://opencode.ai/zen/go/v1/models | head -c 2000`

---

## Phase 3 — Per-job model tracking

**Current State.** `Job.backend_name` (`models.py:55`) records the active backend; there is no
equivalent for the model. The model is resolved per dispatch by `make_backend_factory`'s
`_model_for` closure (`server.py:60-63`). The factory signature is `(name: str) -> AgentBackend`.
Many tests pass `backend_factory=lambda name: FakeAgentBackend(...)`.

**Desired State.** A job pins its own model — its current rung — plus a hop counter, and that
pin reaches the backend constructor without breaking any existing factory caller.

**Problems/Bugs.**
- With no per-job model, a hop has nowhere to record where it moved to.
- **[skeptic] `_wrap_factory` cannot detect the new parameter.** `orchestrator.py:66-75` counts
  only parameters whose `default is inspect.Parameter.empty`, so
  `_backend_factory(name, model=None)` counts **1** — byte-identical to `lambda name: …`.
  Reusing that counter means either every test factory is called with 2 args (loud break) or
  the real factory never receives the model (**silent** — the next dispatch rebuilds the same
  failing model and the hop is lost).
- **[skeptic] No reset path clears the model.** `POST /api/jobs/{id}/reset`
  (`routes_jobs.py:659-682`) → `soft_reset_job` (`repo.py:218-270`) / `nuclear_reset_job`
  (`:273-294`); neither touches `backend_name` today, so neither would touch `model_name`.
  A job that burned its ladder would restart **permanently pinned to the priciest rung it
  reached** — directly against the "no surprise expensive model" requirement.
- The pipeline must not import `server.py` or `Settings` (circular import).

**Solutions.**
- `jsa/db/models.py`: `model_name: Mapped[str | None]` (Text, nullable) **and**
  `model_hops: Mapped[int]` (Integer, default 0). `jsa/db/engine.py`: two more additive
  `ALTER TABLE jobs ADD COLUMN …` in the existing `try/except OperationalError` chain
  (`engine.py:43-47` is the pattern; `model_hops` needs the `NOT NULL DEFAULT 0` form used for
  `retry_count` at `:35`).
- `jsa/server.py::make_backend_factory`: inner factory becomes
  `_backend_factory(name: str, model: str | None = None)`. `_model_for` gains the per-call
  model as the **new highest precedence**, then today's chain unchanged. Documented order:
  per-job rung > `model_override` (`--fit-model`) > `backend_models[name]` > flat default.
- `Orchestrator._wrap_factory`: **detect by parameter name/count over `sig.parameters`, not by
  the no-default counter** — e.g. accept a model argument only when the signature exposes a
  `model` parameter (or ≥2 parameters total). Keep the existing zero-arg legacy branch intact.
  Test both shapes explicitly.
- `_run_one` passes `job.model_name` to `self._backend_factory(...)` and to the
  `fit_backend_factory` call (`orchestrator.py:311-318`). `model_name` stays `None` until the
  first hop — `None` means "whatever the factory resolves", i.e. today's behavior.
- **Reset paths clear both fields**: `soft_reset_job` and `nuclear_reset_job` set
  `model_name = None`, `model_hops = 0`. (Leave `backend_name` alone — that is existing
  behavior and out of scope.)
- Inject `model_ladder: Callable[[str], list[str]] | None = None` and
  `model_resolver: Callable[[str], str | None] | None = None` into `Orchestrator`, built in
  `server.py`. Both default `None` → ladder disabled, which is what every existing test gets.
  **The resolver must be built from the same backend→flat-default mapping `_model_for` already
  encodes** — extract one shared helper rather than writing a second copy. CLAUDE.md warns
  that re-deriving this mapping at a new call site is how the fit gate once got a 600s timeout
  instead of 180s.

**Tests**: both columns round-trip; `make_backend_factory` precedence (per-call model beats
runtime selection; `--fit-model` beats a per-call model); `_wrap_factory` handles zero-arg,
one-arg, and `(name, model=None)` factories correctly — the one-arg case must **not** receive a
model; both reset endpoints clear `model_name`/`model_hops`.

---

## Phase 4 — Model-first fallback in the BF-19 funnel

**Current State.** All three BF-19 exceptions funnel into `_advance_backend_or_fail`
(`orchestrator.py:436-503`), which advances `self._backends[idx+1]` via
`repo.backend_switch_reset` + `BackendSwitchedEvent`, or marks the job failed.

**Desired State.** For *availability* failures, the funnel first tries the next model on the
current backend; only when the ladder is spent does it advance the backend, resetting the model
so the new backend starts at its own entry point.

**Problems/Bugs.**
- Today a busy model burns a whole backend — with a two-backend OpenCode chain, two busy models
  kill the job. That is what happened to EPAM and Innowise.
- Advancing the backend without clearing `model_name` would carry a model ID the new backend
  does not host (`opencode-go` validates against `_PROTOCOL` and raises a bare `ValueError` —
  none of BF-19's three recognised exceptions).
- `google-cli` has no model concept (`SUPPORTS_MODEL_SELECTION["google-cli"] = False`).
- **[skeptic] `backend_switch_reset` commits internally** (`repo.py:404-406`), so assigning
  `job.model_name` *after* calling it is a second, uncommitted transaction — violating
  CLAUDE.md's Checkpoint rule and silently losing the hop, which turns "bounded by the ladder"
  into an unbounded `pending → running → fail → hop` cycle on the same model.
- **[skeptic] A hop discards the in-flight conversation**, so the model re-asks the user's
  question: the reset deletes the stage's `Message` rows and clears the session ID; answered
  `FollowUp`s survive (`repo.py:394-397`) but nothing replays them into the fresh session.
- **[skeptic] An exception inside the new branch parks the job in `running` forever.**
  `_advance_backend_or_fail` wraps its whole body in `except Exception: logger.error(...)`
  (`:498-503`), `list_runnable_jobs` has no `running` arm, and `_run_one`'s `finally` releases
  the semaphore regardless — so a raising ladder callable leaves an invisible zombie job
  recoverable only by `recovery_sweep` on restart.

**Solutions.** Insert a ladder branch ahead of the backend advance:

1. **Eligibility.** Ladder applies only to `AgentTimeout` and `AgentBackendUnavailable`.
   `AgentLimitReached` skips straight to the backend advance — plumb the exception kind through
   the existing thin wrappers (`_handle_limit_reached` / `_handle_backend_timeout` /
   `_handle_backend_unavailable`) as a `use_ladder: bool`.
2. Resolve `current_backend = job.backend_name or self._backends[0]` and
   `current_model = job.model_name or self._model_resolver(current_backend)`.
3. **Two brakes, both required to pass:**
   - **Hop cap:** `job.model_hops < 5`.
   - **Answered-input brake:** if the failed stage has ≥1 answered `FollowUp`, allow the hop
     only when `job.model_hops == 0`. Counting hops (rather than testing `model_name is not
     None`) is what makes this survive the two bypasses the skeptic found — the `revising_*`
     reset deletes **answered** FollowUps too (`repo.py:338-344`, explicitly commented), and a
     `cv_adjust` rewind to `pending` means the *next* failure is at `fit_assessment`, a stage
     that has no FollowUps at all.
   - **Pinned-fit escape:** if `failed_stage == fit_assessment` **and** a `--fit-model` pin is
     in effect, skip the ladder entirely and advance the backend. Otherwise the pinned model
     keeps failing, the ladder hops the *general* model, `cv_adjust` rewinds to `pending`, fit
     re-runs on the same pinned failing model — zero forward progress across the whole ladder.
4. If eligible **and** `SUPPORTS_MODEL_SELECTION[backend]` **and** `next_model(...)` returns a
   rung → call `repo.backend_switch_reset(session, job, current_backend, failed_stage,
   new_model_name=next_rung)`. **Add that optional kwarg to `backend_switch_reset`** so the
   model and the hop-counter increment land in its existing single commit — the only way to
   satisfy the Checkpoint rule. Emit a `LogEvent` and a new
   `ModelSwitchedEvent{job_id, backend, from_model, to_model}` (`jsa/events/schema.py`,
   alongside `BackendSwitchedEvent:62`).
5. Otherwise fall through to today's backend advance, additionally setting `model_name = None`
   and `model_hops = 0` (same kwarg, same commit).
6. **Fail-safe:** wrap the ladder branch in its own `try/except` that falls through to the
   backend advance on any error, so a ladder bug degrades to today's behavior instead of
   creating a zombie `running` job.
7. Chain fully exhausted → today's `mark_failed`, message extended to say both models and
   backends were tried.

**Known interaction, documented not fixed.** A `cv_adjust` hop rewinds to `pending`, re-running
`fit_assessment` — already documented as "an extra fit turn, by design". The ladder multiplies
that, which is why the hop cap exists.

**Tests** (extend `tests/backend/test_bf18_limit_detection.py`): a timeout advances the *model*
and keeps `backend_name`; **assert by re-reading the job from a fresh DB session**, not the
in-memory ORM object, or a lost commit passes silently; `AgentBackendUnavailable` behaves the
same; **`AgentLimitReached` does NOT hop** and advances the backend immediately; the last rung
advances the backend and nulls `model_name`/`model_hops`; `google-cli` skips the ladder; the
hop cap stops at 5; the answered-input brake allows exactly one hop and survives a `revising_*`
reset; a pinned `--fit-model` + fit failure skips the ladder; **a ladder callable that raises
still reaches the backend advance rather than leaving the job `running`**.

---

## Phase 5 — Observability and docs

**Current State.** `BackendSwitchedEvent` is the only fallback-visible event.
`frontend/src/components/Header.tsx:270-288` reconciles an active-backend indicator from the
most-recently-updated job's `backend_name`; `:290-300` populates `selectedModels` from
`GET /api/backend-models` — the **global** selection, which a ladder hop never touches.

**Desired State.** The user can see which model a job is actually on, without that display
fighting the dropdown.

**Problems/Bugs.**
- **[skeptic] Two truths, no reconciliation.** The dropdown shows the user's selection while a
  hopped job runs a different rung. Mirroring the `backend_name` reconciliation would let a hop
  silently rewrite the displayed *selection*. And `model_name` is `None` until the first hop, so
  a naive display is blank — while the obvious fix, seeding it at dispatch, would corrupt
  Phase 4's hop accounting.

**Solutions.**
- **Pick the lane explicitly: display only, never write back.** The job DTO exposes an
  *effective model* = `job.model_name` if set, else the backend's currently-configured model.
  It renders on the **job row** (per-job truth), never into the header dropdown, which keeps
  meaning "your global selection". `model_name` is **never** seeded at dispatch.
- `ModelSwitchedEvent` added to `jsa/events/schema.py` and handled next to `backend_switched`;
  the Phase 4 `LogEvent` already renders in the job log with no frontend change.
- New UI strings via `frontend/src/i18n/strings.en.json` + `useT()` per CLAUDE.md, then
  `scripts/translate-ui.sh`, then **`npm run build`** (the CLI serves the built bundle).
- CLAUDE.md: extend "Backend fallback chain (BF-19)" with a **"Model ladder (tried before the
  backend advance)"** subsection covering escalation order; why the hop is a job-level rewind;
  why `AgentLimitReached` is excluded; the curated-catalog choice; the `google-cli` exemption;
  the pinned-fit escape; both brakes and why counting hops (not `model_name`) is what makes the
  answered-input brake sound; that the `opencode-go` ladder is a **protocol-crossing** hop; and
  why the ladder does not contradict the "don't blindly repeat an expensive call" directive.

---

## Verification

1. `pip install -e .`, then `pytest -v -m "not integration"` — full backend suite green.
2. `cd frontend && npm test`, then **`npm run build`**.
3. Targeted: `pytest tests/backend/test_bf18_limit_detection.py tests/backend/test_model_costs.py tests/backend/test_orchestrator_throttling.py -v`
4. **Diagnostic that validates the whole diagnosis** — check whether the three original
   failures were quota or timeout, since the two now take different paths. The three
   `exhausted_message` strings (`orchestrator.py:398/413/431`) are distinguishable:
   `sqlite3 ~/.jsa/jsa.sqlite "select company, substr(error,1,80) from jobs where state='failed'"`
   If they say "limit reached", the 429 exclusion means the ladder will *not* engage for them
   and Phase 1 is doing all the work — worth knowing before judging the feature.
5. **Live repro** — re-run the three jobs with `--backends opencode-zen,opencode-go`. Expect a
   model-switch line *before* any backend-switch line, and at most 2 concurrent jobs per backend.
6. Confirm the OpenCode cost probe from Phase 2 (needs your exported `OPENCODE_API_KEY`).
7. Playwright visual pass on the Phase 5 job-row model display, if installed.

## Change Log

_(entries appended as phases land)_

## Decisions Log

_(user-authored only)_
