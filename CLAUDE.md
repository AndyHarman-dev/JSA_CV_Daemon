# CLAUDE.md — JSA Project Conventions

This file records non-derivable conventions for all agents working on this project. Architecture details live in `ARCH.md`; implementation status lives in `PLAN.md`. Only things that would be unclear from reading the code belong here.

---

## Sentinel protocol — MANDATORY for all agent prompts

Every prompt file (`jsa/prompts/PROMPT_CDADJUST.md`, `jsa/prompts/CVL_PROMPT.md`) **must** instruct the model to terminate every reply with exactly one of:

```
<<<NEED_INPUT>>>
<question to the user>
<<<END>>>
```
or
```
<<<FINAL>>>
<final markdown payload>
<<<END>>>
```

This is not optional — the pipeline parser (`jsa/agents/protocol.py`) will raise `ProtocolError` and mark the job `failed` if the sentinel is absent or malformed. When writing or editing prompt stubs, always include this instruction prominently at the end of the system prompt.

---

## State transitions

**Never set `Job.state` or `Job.current_stage` directly.** Always go through:
```python
from jsa.pipeline.state_machine import transition
transition(job, new_state, new_stage=None)  # raises InvalidTransition on violation
```
The allowed-transitions table is the single source of truth in `jsa/pipeline/state_machine.py`.

---

## Checkpoint rule

Every write that changes job state must be a **single atomic DB transaction** via:
```python
await repo.checkpoint(session, job, new_state, new_stage, messages=[], document=None, follow_up=None)
```
Never write `Message` rows, `Document` rows, and `Job` state in separate commits.

---

## Agent backend registration

New backends are registered in `jsa/agents/registry.py` by adding an entry to the `_REGISTRY` dict. The key is the CLI-flag string (e.g., `"claude-cli"`, `"google-cli"`, `"anthropic"`). Backends must subclass `AgentBackend` and implement all four abstract methods.

---

## Backend fallback chain (BF-19)

`Settings.backends` (`jsa/config.py`) is an **ordered list**, not a single value —
`backends[0]` is the primary and the rest form a fallback chain. CLI: `--backends a,b,c`
sets the ordered chain; `--backend x` is a **backward-compat alias** for a single-item
chain, and `--backends` takes precedence when both are given (`jsa/cli.py`). Each `Job`
tracks its active backend in the `Job.backend_name` column; the orchestrator assigns
`backends[0]` on first dispatch.

When a backend raises `AgentLimitReached` (e.g. `jsa/agents/claude_cli.py` on a quota/rate
signal), `AgentTimeout` (e.g. `jsa/agents/opencode_zen.py`'s `httpx.TimeoutException`
handler, or any CLI backend's `run_killable` timeout), **or `AgentBackendUnavailable`**
(`jsa/agents/base.py` — a non-timeout, non-quota failure where retrying the SAME backend
won't help: a bad model/config, an auth error, or a transient overload/gateway failure that
already exhausted its own in-backend retry budget — see "OpenCode Zen backend" below),
`Orchestrator._run_one` (`jsa/pipeline/orchestrator.py`) routes all three into the shared
`_advance_backend_or_fail` helper — **advances the job to the next backend in the chain**
via `repo.backend_switch_reset` (resets to the failed stage, preserving the checkpoint),
emitting a `BackendSwitchedEvent`. Only when the chain is **exhausted** is the job
`mark_failed`'d, with a message specific to which of the three tripped: "Backend limit
reached — switch backends or wait for quota reset", "Backend timed out on every configured
backend — switch backends or increase the timeout", or "Backend unavailable on every
configured backend — check model/API key configuration, or try again later if this was
transient overload". Do not treat any of the three as a hard job failure; that is the
chain's job. (`_handle_limit_reached` / `_handle_backend_timeout` /
`_handle_backend_unavailable` are thin wrappers over `_advance_backend_or_fail` that only
differ in these message strings — before the timeout fix, `AgentTimeout` had no wrapper at
all and fell straight into `_run_one`'s generic `except Exception`, hard-failing the job on
the very first backend even with a working fallback configured in `--backends`.)

**`_run_fit_assessment` (`jsa/pipeline/stages.py`) catches `ProtocolError` ONLY —
`AgentTimeout`, `AgentLimitReached`, and `AgentBackendUnavailable` must propagate through
it.** `fit_assessment` is the
FIRST stage every job hits, on the same backend/timeout as every other stage. A timeout or
quota signal there means the backend didn't answer — it is not the model saying "not a fit".
Catching it here and parking the job at `unfit` would fabricate a verdict about the user's
application from what is actually a transport failure, AND would mean the fit gate never
lets BF-19 try the next configured backend — a chain like `--backends opencode-zen,claude-cli`
would silently never reach claude-cli whenever opencode-zen was slow, because every job dies
at the very first stage before the chain logic downstream ever runs. `ProtocolError` (a
malformed/sentinel-less reply the model actually sent) is the only case that still fails to
the `unfit` modal — that is a real "unparseable answer", not an absent one. Do not widen this
except clause back to include `AgentTimeout`/`AgentLimitReached` as a "simplification"; that
was the exact shape of a real bug (see `tests/backend/test_fit_assessment.py`'s
`test_agent_timeout_propagates_for_bf19_not_swallowed_to_unfit` and
`test_timeout_from_fit_backend_switches_to_next_backend_not_unfit`).

One real, separate interaction to be aware of (not a bug): a `cv_adjust` failure's BF-19
rewind target is `pending` (`repo.backend_switch_reset`'s `failed_stage` mapping, docstring
in `jsa/db/repo.py`), so after a `cv_adjust`-stage switch the retry re-enters
`fit_assessment` on the new backend before reaching `cv_adjust` again — an extra fit turn,
by design, not a loop.

### OpenCode Zen backend

`opencode-zen` (`jsa/agents/opencode_zen.py`) is an HTTP backend, not a CLI one — it POSTs
to `https://opencode.ai/zen/v1/chat/completions`, an **OpenAI-compatible** chat-completions
endpoint (not the Anthropic Messages API shape `anthropic_api.py` uses): the system prompt
is the first `{"role": "system", ...}` entry in `messages`, not a top-level `system` kwarg,
and the reply lives at `body["choices"][0]["message"]["content"]`. Auth is
`Authorization: Bearer $OPENCODE_API_KEY`, read from the environment at call time (never
hardcoded) — same convention as `ANTHROPIC_API_KEY`; this project loads no `.env` file
itself, so the caller must export it first.

Its model catalog is unrelated to JSA's Claude-only `model` setting (it proxies Claude,
GPT, Gemini and various free-tier models — e.g. the default `nemotron-3-ultra-free`), so
it gets **dedicated** settings, `opencode_zen_model` / `opencode_zen_timeout`
(`JSA_OPENCODE_ZEN_MODEL` / `JSA_OPENCODE_ZEN_TIMEOUT`), rather than sharing `model` /
falling back to `settings.model` the way `claude-cli` does.

**The API can return `HTTP 200` with an error payload in the body** (observed live: a
transient upstream 502 from the underlying provider surfaces as `{"error": {"type":
"server_error", ...}}` with `response.status_code == 200`). `_call_api_once` therefore
checks for an `"error"` key in the parsed body regardless of status code, not just `>= 400`
/ `429` — do not "simplify" this to a plain status-code check, it will silently swallow
these.

**Three-way error classification (`_call_api_once`, wrapped by `_call_api`'s retry loop).**
The free `nemotron-3-ultra-free` model is genuinely flaky under upstream load — intermittent
5xx gateway errors, a `{"error": {"type": "server_error"}}` envelope, or a null message
content are all things this specific backend is known for, and the right response is to
retry the SAME backend a couple of times before giving up on it, not to switch backends on
the first hiccup:

1. **Quota/rate** — `"rate"` / `"credit"` in the error message, `err_type` in
   `{"RateLimitError", "CreditsError"}`, or HTTP `429` → `AgentLimitReached`, immediately,
   no retry. Unchanged from before.
2. **Transient overload/gateway** — a structured JSON error body at any status `< 400`
   (the live-observed transient-upstream-502 shape is `{"error": {"type": "server_error"}}`
   at HTTP `200`) or `>= 500`, a non-JSON body at `>= 500`, or a null `message.content` →
   raises the module-private `_TransientOpenCodeError`, which `_call_api`'s loop retries up
   to `_MAX_ATTEMPTS` (3, with a `1.0s`/`3.0s` backoff) on the *same* backend before
   converting the final failure into `AgentBackendUnavailable`. This is "handled as
   gracefully as possible" per the user's framing — the backend gets its own retries before
   BF-19 gives up on it. **Deliberately not keyed on a guessed set of `err_type` strings**
   (e.g. `"overloaded_error"`) — the only *confirmed* live shape is the 200-status
   `server_error` envelope, so anything outside a real `4xx` defaults to transient rather
   than risking an unrecognized-but-actually-transient error type silently skipping retries.
3. **Permanent config/client error** — a structured JSON error body at a real `4xx` status
   (e.g. an `invalid_request_error` for a bad model name), or a non-JSON `4xx` →
   `AgentBackendUnavailable` immediately, with **no retry** — a bad `--opencode-zen-model`
   or a malformed request will never succeed by calling the same backend again, so retrying
   would only burn `_MAX_ATTEMPTS` × timeout of wall-clock time before reaching the same
   conclusion BF-19 could have reached immediately.

`AgentBackendUnavailable` (both from (2) exhausting its retries and from (3)) still joins
the BF-19 fallback chain exactly like `AgentLimitReached`/`AgentTimeout` — see "Backend
fallback chain (BF-19)" above. `AgentTimeout` (an `httpx.TimeoutException`, i.e. the request
already burned a full `self._timeout`) is deliberately **not** part of the in-backend retry
loop — repeating an expensive multi-minute call blindly before even trying the next backend
would be a poor trade; it keeps its own separate, unretried BF-19 path. Do not fold `429` /
quota signals into the retry loop either — those are `_TransientOpenCodeError`'s opposite
case, a signal that this backend specifically cannot serve the request right now, not "try
again in a second."

Regression coverage: `tests/backend/test_opencode_zen.py`'s
`TestCallApiRetryClassification` (retries-then-succeeds, retries-exhausted, and each
immediate/no-retry case) and `tests/backend/test_bf18_limit_detection.py`'s
`TestOrchestratorBackendUnavailableDetection` (the orchestrator-level BF-19 switch/exhaust
behavior). Do not "simplify" the three-way split back into a single `RuntimeError`/
`AgentLimitReached` pair — that was the exact shape of the reported bug: a transient
overload or a bad model both used to raise a plain `RuntimeError` that `_run_one`'s generic
`except Exception` hard-failed on the very first backend, with `--backends
opencode-zen,claude-cli` never trying `claude-cli`.

**Sentinel-compliance nudge is a different concern and is in scope.** The same free model
also, independently of any transport error, sometimes answers in full, well-formed prose —
including asking exactly the NEED_INPUT-shaped question the prompt wants — and simply never
wraps it in `<<<...>>>...<<<END>>>` (`finish_reason: "stop"`, confirmed live against
production payloads; not truncation). `OpenCodeZenBackend._parse_with_nudge` catches
`parse_reply`'s `ProtocolError("no sentinel block")` specifically and replays the
conversation plus one correction turn before giving up — this mirrors
`ClaudeCliBackend._parse_with_nudge` (`jsa/agents/claude_cli.py`) exactly, including the
same "propagate any other ProtocolError, or a second failure, immediately" rule. Before this
existed, a single non-compliant reply from this backend was an *uncaught* `ProtocolError`
that `run_stage`'s generic exception handler turned straight into a hard `mark_failed("no
sentinel block")` on the job's very first turn — no BF-19 fallback-chain engagement, because
`ProtocolError` doesn't map to `AgentLimitReached`. Do not remove this nudge as a
"simplification"; it is the fix for that failure mode, not a violation of the paragraph
above.

---

## Prompt files

- Location: `jsa/prompts/PROMPT_CDADJUST.md`, `jsa/prompts/CVL_PROMPT.md`, and `jsa/prompts/PROMPT_FIT_ASSESSMENT.md`
- Loaded by `jsa/prompts/loader.py` → `read_prompt(name)` — **no caching**, always reads from disk
- Edited externally by the user — never programmatically overwritten
- During development, use the stubs (which already contain the sentinel grammar instructions)

---

## Fit-assessment gate

Every pending job first runs a one-shot `fit_assessment` stage (always on) before
`cv_adjust`. The agent returns a single `<<<FINAL>>>` whose **first line is `FIT` or
`UNFIT`** (the rest is a brief reason — never `NEED_INPUT`). `FIT` → `fit_done`
(continues to cv_adjust); anything else (UNFIT, unclear, unparseable, or a question) →
`unfit`, with the reason stored in the `Job.fit_reason` column and surfaced as a centered
"not a fit" modal. **Verdict parsing fails *to* the modal (closed), never silently past
it.** The modal's buttons map to `POST /api/jobs/{id}/dismiss` (→ dismissed) and
`POST /api/jobs/{id}/ignore-fit` (→ fit_done → resume pipeline). `fit_done` is treated
exactly like `cv_done` in `list_runnable_jobs` and `_next_stage_for`. See ARCH.md →
"Fit-assessment gate".

### Separate fit-assessment model

The fit gate may run on a **different model** than the rest of the pipeline — it is a
cheap one-shot pre-check, so a smaller/faster model is usually enough. Two optional
settings drive it (`jsa/config.py`), both `None` by default, which makes the whole
feature **inert**: `fit_model` (`JSA_FIT_MODEL` / `--fit-model`) and `fit_timeout`
(`JSA_FIT_TIMEOUT` / `--fit-timeout`). When unset, the fit stage uses exactly the same
backend configuration as every other stage.

The mechanism is `make_backend_factory`'s (`jsa/server.py`) `model_override` /
`timeout_override` kwargs. **Do not derive the constructor arguments yourself at a new
call site** — the per-backend mapping is non-obvious (`anthropic` takes
`anthropic_timeout`, CLI backends take `agent_timeout`, `opencode-zen` takes
`opencode_zen_timeout` and never falls back to `settings.model` — see "OpenCode Zen
backend" above — and `google-cli` takes no `model` at all because the `agy` CLI has no
model flag, so `fit_model` is silently inapplicable there). Overriding without going
through the factory is what previously gave `anthropic` a 600s timeout instead of its
180s one.

Wiring: `server.py` builds a second factory and passes it to `Orchestrator` as
`fit_backend_factory`; the orchestrator instantiates it **from `job.backend_name`**
(not `backends[0]`, so the fit gate follows a BF-19 backend switch) and only for the
`fit_assessment` stage, handing the result to `run_stage(..., fit_backend=...)`.
`run_stage` falls back to `backend` when it is `None`, which is what every test that
omits it gets. **`jsa/pipeline/stages.py` must never import `jsa/server.py`** — server
imports the orchestrator, which imports stages, so that direction is a circular import
that breaks every entrypoint. The fit backend is *injected*, never constructed inside
the pipeline.

---

## CV structure — single source of truth

`cv_structure.json` (`jsa/store/cv_structure.py`, edited via the CV Structure Editor,
`Settings.cv_structure_path` — `~/.jsa/cv_structure.json` by default) is the **only**
source of *base* CV content for the pipeline — i.e. for the stages that haven't yet
produced their own tailored CV. Both `fit_assessment` and `cv_adjust` read it at stage
time (`jsa/pipeline/stages.py::run_stage`) and inject it into their prompts — `cv_adjust`
as the `BASE CV STRUCTURE` JSON skeleton, `fit_assessment` as `cv_to_markdown(structure)`
under a `CV:` header. Neither stage reads `Job.cv_text`. **`cover_letter` is the
exception**: it reads the approved, tailored `cv_adjust` Document instead (falling back to
this base structure only if that Document is somehow missing) — see "Two-lane pipeline /
CV gate" below. Do not "fix" the cover-letter lane back onto `cv_structure.json`; that
would defeat the two-lane split's entire point (writing the letter against what will
actually be submitted).

**`Job.cv_text` is DEPRECATED.** It is never populated (the CLI's `--cv` no longer
stamps it) and never read by any prompt. The column still exists only because
`jsa/db/engine.py`'s migration story is `create_all` + additive `ALTER TABLE` — there is
no column-drop path, and existing sqlite DBs have it `NOT NULL`. Do not read or write it
in new code; do not "fix" this by reintroducing a `CV TEXT:` block into a prompt.

**`--cv` is optional and bootstrap-only.** `jsa/cli.py::_bootstrap_cv_structure` seeds
`cv_structure.json` from `--cv` exactly once, if the file doesn't exist yet (via the same
`jsa/pipeline/infer_structure.py::run_infer` the editor's "infer" button uses). If a
structure already exists, `--cv` is ignored (a note is printed). If neither `--cv` nor a
saved structure exists, startup proceeds anyway — see the gate below.

**CV structure gate.** `Orchestrator.run()` (`jsa/pipeline/orchestrator.py`) checks
`cv_structure_path.exists()` at the top of every dispatch cycle when a path was given
(production always passes one via `server.py`; tests passing `cv_structure_path=None`
are exempt — the gate is inert for them). While the file is missing, the loop skips
dispatch entirely and jobs stay `pending` (never `failed`); a one-shot `LogEvent`
announces the block. `PUT /api/cv-structure` calls `orchestrator.kick()` on save, and
`GET /api/config`'s `cv_structure_exists` field drives the frontend's gate banner
(`frontend/src/components/JobList.tsx`) — so saving a structure in the editor unblocks
pending jobs live, no restart required.

---

## Two-lane pipeline / CV gate

The pipeline runs as two sequential lanes — CV, then cover letter — separated by a parked
gate state, `cv_review`, between them. `jsa/pipeline/state_machine.py` is the source of
truth for the transition table; the shape that matters for new code:

- `running(cv_adjust)` and `running(revising_cv)` land in `cv_review`, never in `cv_done`.
  `cv_review` is a genuine park: the user must call `approve-cv` or request a revision to
  leave it. A bare `cv_review` job (no unconsumed `RevisionRequest`) is **not** dispatched
  by the orchestrator.
- `cv_done` is **unchanged** from before the split: "CV approved, cover letter pending,
  runnable" — the orchestrator dispatches it straight into `cover_letter`. It is reached
  **only** via `POST /api/jobs/{id}/approve-cv` (`cv_review → cv_done`), never directly from
  a finishing `cv_adjust`/`revising_cv` run. `cv_done` also remains the **BF-19 backend-
  fallback rewind target**: when the `cover_letter` stage hits `AgentLimitReached`,
  `Orchestrator._handle_limit_reached` rewinds the job to `cv_done` (not `cv_review`) so the
  next backend re-enters `cover_letter` directly — the CV was already approved, there is
  nothing to re-review. Do not redirect this rewind to `cv_review`.
- A revision's landing state is decided by `RevisionRequest.origin_state`
  (`jsa/pipeline/stages.py`, the `revising_cv`/`revising_cl` branch): a `revising_cv`
  completion returns to `cv_review` if `origin_state == "cv_review"`, otherwise to `review`
  (also the fallback for legacy `NULL` rows). `revising_cl` always returns to `review` — there
  is no `cl_review` parked state; the cover letter has no gate of its own.
- The `cover_letter` stage's initial prompt is built against the **approved `cv_adjust`
  Document's rendered markdown** (`"TAILORED CV (approved by the user — write the letter
  against this)"` block, `jsa/pipeline/stages.py`), not `cv_structure.json` — the letter is
  written against what will actually be submitted. Falling back to the base structure only
  happens if no `cv_adjust` Document exists yet, which the CV gate makes unreachable in
  normal flow; that branch logs a warning if hit.
- `fit_done` is unaffected by any of this — it still parallels `cv_done` as "ready to be
  picked up for the next stage" (`cv_adjust`), and is still treated identically to `cv_done`
  in `list_runnable_jobs`/`_next_stage_for`. See "Fit-assessment gate" above.
- `cv-research` (`jsa/prompts/GEMINI_CV_RESEARCH.md`) and `cl-research`
  (`jsa/prompts/GEMINI_CL_RESEARCH.md`) are prompt files only — there is no wired agent
  backend that invokes `cv-research`; it is retained deliberately for a future CV-lane
  research pass, not dead code to delete. `cl-research` **is** wired (`_gather_research` in
  `jsa/pipeline/stages.py`, called for the `cover_letter` stage only).

---

## Testing conventions

- **Fakes over mocks.** Use `tests/backend/fakes/fake_backend.py` (`FakeAgentBackend`) for all pipeline and orchestrator tests. Do not `patch` or `MagicMock` internal functions.
- **FakeRenderer** should write a stub file (e.g., write `b"PDF"` to the output path) — do not skip the renderer call in tests.
- Integration tests (real Claude/Gemini/Anthropic API) live in `tests/backend/integration/`, are marked `@pytest.mark.integration`, and are **skipped by default in CI**.
- Backend tests: `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`.
- Frontend tests: `vitest` + `@testing-library/react`.
- Follow TDD: write tests before or alongside implementation, not after.

---

## Renderer invocation

Renderers (`WeasyPrintRenderer` for PDF, `DocxRenderer` for DOCX) fire at **three** points
in `jsa/pipeline/stages.py`, all via the shared `_render` helper:

1. **`_render_cv`** (CV-only, `stages=(Stage.cv_adjust,)`) — on entry to `cv_review`, i.e.
   `cv_adjust`'s first FINAL and every `revising_cv` completion that returns to `cv_review`.
   Only the CV is rendered here; there is no cover-letter Document yet.
2. **`_render_for_review`** (both artifacts, `stages=(Stage.cv_adjust, Stage.cover_letter)`)
   — on entry to `review`: `cover_letter`'s first FINAL, and every `revising_cv`/`revising_cl`
   completion whose `RevisionRequest.origin_state` was `review` (not `cv_review`).
3. Manual re-render of an already-approved job via `POST /api/jobs/{id}/export`.

`_render` itself skips any stage with no Document yet rather than erroring — this is what
lets step 1 render CV-only without special-casing the missing cover letter.

The frontend preview is a PDF `<iframe>` fed by `GET /api/files/{relpath}` (served
`Content-Disposition: inline`), not in-browser Markdown. Neither `POST /api/jobs/{id}/approve-cv`
(`cv_review → cv_done`) nor `POST /api/jobs/{id}/approve` (`review → approved`) does **any**
rendering — both are pure state transitions; the render already happened on entry to the
state they leave. Do not move rendering onto either approve endpoint, and do not treat the
CV-gate or review-entry pre-renders as a bug. See ARCH.md → "Renderer runs on review entry
(pre-render)" (predates the CV gate; the principle — render on park, not on approve — now
applies at both parking states).

---

## Concurrency — do not block the event loop

Any synchronous, CPU-bound, or blocking I/O call must be wrapped:
```python
await asyncio.to_thread(sync_function, *args)
```
This applies to: WeasyPrint, python-docx parsing, pypdf parsing, and any future sync library.

---

## Job identity and re-run semantics

- Job primary key: `sha1(f"{company}|{role}|{link}")[:16]`
- JD hash: `sha1(jd)[:16]`
- On re-run: `approved` + unchanged `jd_hash` → skip. `jd_hash` changed → reset to `pending`. `failed` → reset to `pending`. Everything else → resume from last checkpoint.

---

## Port and local-only

- Default port: `8765`. Override via `--port` flag or `JSA_PORT` env var.
- CORS is wide-open for `http://localhost:*`. This is intentional — single-user, local-only tool.
- No auth, no multi-user, no remote deployment ever intended for v1.

---

## Language preference

A single global setting (`jsa/store/preferences.py`, `GET`/`PUT /api/preferences`) drives
three things: (1) the LLM pipeline's output language for the CV/cover-letter JSON, its
clarifying questions, and its change-log; (2) the fit-assessment reason text; (3) the whole
frontend's UI chrome. The language catalog (`jsa/i18n/languages.py`) is the single source of
truth — served to the frontend via `/api/config`'s `languages` key; never hand-copy the list
into TypeScript.

**Pipeline directive.** `jsa/pipeline/stages.py::_with_language_directive` appends a language
directive to a **NEW** session's system prompt only (`start_session` call sites: fresh
cv_adjust/cover_letter, `_run_fit_assessment`) — never to a `restore_session` call (a resumed
session already committed to a language; re-injecting a changed directive would contradict
replayed history). The directive carves out two things that must always stay
English/ASCII: the `<<<FINAL>>>`/`<<<NEED_INPUT>>>`/`<<<END>>>` sentinels, and (for
`fit_assessment` only) the literal `FIT`/`UNFIT` verdict word matched by `_parse_fit_verdict`.
`jsa/schema/cv.py`'s `_not_a_cover_letter` guard is per-language
(`_LETTER_FORMULA_RE_BY_LANG`, selected via `ValidationInfo.context["language"]`) — when
adding a new language to the catalog that pipeline output may actually use, add its letter-
formula tuple too, or the guard silently falls back to English-only matching for that
language.

**Frontend i18n (UI string convention — applies to all future frontend work, not just this
feature).** Any new user-visible frontend string **must**:
1. Be added to `frontend/src/i18n/strings.en.json` with a stable dotted key
   (`"<component>.<label>"`, e.g. `"jobList.emptyState"`).
2. Be rendered via `const t = useT();` (`frontend/src/i18n/useT.ts`) — never a hardcoded
   English literal in JSX. `useT()` reads the global `language` store field and falls back to
   English, then the raw key, if a translation is missing.
3. Trigger a re-run of `scripts/translate-ui.sh` (incremental — only translates new/changed
   keys) before shipping, so generated locale catalogs (`strings.<code>.json`) stay in sync.
   Two backends, mirroring `jsa/agents/anthropic_api.py` vs `jsa/agents/claude_cli.py`:
   `--backend api` (default, needs `ANTHROPIC_API_KEY`) or `--backend cli` (shells out to the
   Claude Code CLI, no API key needed). Use `scripts/translate-ui.sh --check` in CI/pre-merge
   to catch a catalog that's fallen behind.

Do not externalize: CSS class names, `data-testid`, console/log strings, dates/numbers/IDs/
URLs, or HUD-style terminal abbreviations that are intentionally code-like (see
`frontend/src/theme/chrome.tsx`'s `StateMeta.code` vs `.label`).

---

## How to test a phase

After each phase is implemented, verify it with the following steps in order.

### 1. Install / sync the Python package

```bash
pip install -e .
```

Re-run this whenever `pyproject.toml` dependencies change (i.e., after Phase 1 and any phase that adds a new dep).

### 2. Run the backend test suite

```bash
pytest -v
```

Or, to run only the tests added in the current phase:
```bash
pytest tests/backend/test_<phase_module>.py -v
```

All tests must pass. `asyncio_mode = "auto"` is set in `pyproject.toml` — no extra flags needed for async tests.

To skip slow integration tests (default):
```bash
pytest -v -m "not integration"
```

### 3. Run the frontend test suite (phases 9–10 onward)

```bash
cd frontend
npm install        # first time or after package.json changes
npm test           # runs: vitest run
```

### 4. Manual smoke-test the CLI (Phase 1+)

```bash
# Verify basic invocation prints the scaffold message
jsa --csv /path/to/jobs.csv --cv /path/to/resume.pdf

# --cv is optional — it only seeds cv_structure.json once, if none exists yet.
# Jobs stay pending (gated, not failed) until a structure exists — see CLAUDE.md
# → "CV structure gate".
jsa --csv /path/to/jobs.csv

# Verify validation rejects bad inputs (the --cv extension check only fires when --cv is given)
jsa --csv jobs.txt --cv resume.pdf         # should error: bad csv extension
jsa --csv jobs.csv --cv resume.txt         # should error: bad cv extension
jsa --csv jobs.csv --cv resume.pdf --backend bad  # should error: bad backend
```

### 5. Manual smoke-test the server (Phase 8+)

```bash
jsa --csv jobs.csv --cv resume.pdf --no-browser
# Open http://localhost:8765/api/health in a browser or curl:
curl http://localhost:8765/api/health      # should return {"ok": true}
curl http://localhost:8765/api/jobs        # should return []
```

### 6. Manual smoke-test the frontend dev server (Phase 9+)

```bash
cd frontend && npm run dev
# Open http://localhost:5173 in a browser
```
