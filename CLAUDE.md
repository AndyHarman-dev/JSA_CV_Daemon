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
signal), `Orchestrator._handle_limit_reached` (`jsa/pipeline/orchestrator.py`) **advances
the job to the next backend in the chain** via `repo.backend_switch_reset` (resets to the
failed stage, preserving the checkpoint), emitting a `BackendSwitchedEvent`. Only when the
chain is **exhausted** is the job `mark_failed`'d with "Backend limit reached — switch
backends or wait for quota reset". Do not treat a limit signal as a hard job failure; that
is the chain's job.

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

---

## CV structure — single source of truth

`cv_structure.json` (`jsa/store/cv_structure.py`, edited via the CV Structure Editor,
`Settings.cv_structure_path` — `~/.jsa/cv_structure.json` by default) is the **only**
source of CV content for the pipeline. Both `fit_assessment` and `cv_adjust` read it at
stage time (`jsa/pipeline/stages.py::run_stage`) and inject it into their prompts —
`cv_adjust` as the `BASE CV STRUCTURE` JSON skeleton, `fit_assessment` as
`cv_to_markdown(structure)` under a `CV:` header. Neither stage reads `Job.cv_text`.

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

## Testing conventions

- **Fakes over mocks.** Use `tests/backend/fakes/fake_backend.py` (`FakeAgentBackend`) for all pipeline and orchestrator tests. Do not `patch` or `MagicMock` internal functions.
- **FakeRenderer** should write a stub file (e.g., write `b"PDF"` to the output path) — do not skip the renderer call in tests.
- Integration tests (real Claude/Gemini/Anthropic API) live in `tests/backend/integration/`, are marked `@pytest.mark.integration`, and are **skipped by default in CI**.
- Backend tests: `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`.
- Frontend tests: `vitest` + `@testing-library/react`.
- Follow TDD: write tests before or alongside implementation, not after.

---

## Renderer invocation

Renderers (`WeasyPrintRenderer` for PDF, `DocxRenderer` for DOCX) are invoked by `_render_for_review` (`jsa/pipeline/stages.py`) when a job **enters `review`** — on cover-letter completion and on every revision completion — rendering both CV and cover letter to **both PDF and DOCX**. The frontend preview is a PDF `<iframe>` fed by `GET /api/files/{relpath}` (served `Content-Disposition: inline`), not in-browser Markdown. `POST /api/jobs/{id}/approve` does **no** rendering — it only transitions `review → approved`. An approved job may be re-rendered on demand via `POST /api/jobs/{id}/export`. Do not move rendering back onto `approve`, and do not treat the review-entry pre-render as a bug. See ARCH.md → "Renderer runs on review entry (pre-render)".

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

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
