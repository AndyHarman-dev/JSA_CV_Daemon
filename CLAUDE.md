# CLAUDE.md — JSA Project Conventions

This file records non-derivable conventions for all agents working on this project. Architecture details live in `ARCH.md`; implementation status lives in `PLAN.md`. Only things that would be unclear from reading the code belong here.

---

## Implementation Workflow

Whenever asked to implement a feature, a plan phase, or any multi-step task:

1. **Draft a todo list first.** Before writing any code, create a file at `~/.claude/todos/<feature-slug>.md` listing every concrete step as a checkbox. Example path: `~/.claude/todos/excel-table-migration.md`.
2. **Work through the list.** After completing each step, tick its checkbox (`- [x]`) by editing the file, then continue to the next item. Keep the file as a live checklist throughout the session so the current state is always visible.
3. **Clean up on approval.** Once the user confirms the implementation is accepted, delete the todo file with `rm ~/.claude/todos/<feature-slug>.md`.

If a session is interrupted before approval, leave the todo file in place so the next session can resume from where it left off.

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

# Verify validation rejects bad inputs
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
