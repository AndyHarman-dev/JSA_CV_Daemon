# AGENTS.md — JSA Project

## Commands

```bash
pip install -e .                       # install jsa package (editable)
pytest -v -m "not integration"         # backend tests (default, skips real API)
pytest tests/backend/test_X.py -v     # single test file
jsa --csv jobs.csv --cv resume.pdf     # run app (port 8765, opens browser)
jsa --csv jobs.csv --cv resume.pdf --no-browser  # headless
jsa --csv jobs.csv --cv resume.pdf --backends claude-cli,google-cli  # ordered backend chain
cd frontend && npm run dev             # FE dev server (port 5173)
cd frontend && npm test                # vitest
cd frontend && npm run build           # tsc + vite build → jsa/static/
```

**Env**: `JSA_PORT` overrides default port `8765`.

## Architecture spine

- **CLI entry**: `jsa/cli.py` (typer) — parses args, inits DB, starts uvicorn + orchestrator in-process.
- **FastAPI**: `jsa/server.py` — routes, WS, static FE bundle mount.
- **Orchestrator**: `jsa/pipeline/orchestrator.py` — `asyncio.Semaphore(5)` dispatcher loop.
- **Pipeline stages**: `jsa/pipeline/stages.py` — `fit_assessment` → `cv_adjust` → `cover_letter` → review.
- **DB**: SQLite at `~/.jsa/jsa.sqlite` via `SQLAlchemy 2.0` + `aiosqlite`. No migrations — `create_all()` on startup.
- **Backends**: registered in `jsa/agents/registry.py` under keys `"claude-cli"`, `"google-cli"`, `"anthropic"`.

## Non-obvious rules (violations break things)

- **Never set `Job.state`/`Job.current_stage` directly.** Use `pipeline.state_machine.transition(job, new_state, new_stage=None)`.
- **Atomic DB writes.** Every job-state change goes through `repo.checkpoint(session, job, new_state, new_stage, messages=[], document=None, follow_up=None)` — single commit for Message rows + Document + Job state.
- **Sentinel protocol is mandatory.** Every prompt must instruct the model to end replies with `<<<NEED_INPUT>>>...<<<END>>>` or `<<<FINAL>>>...<<<END>>>`. Violation → `ProtocolError` → job fails.
- **`asyncio.to_thread()` for sync libs.** WeasyPrint, python-docx, pypdf are all sync and blocking — must be wrapped.
- **Renderer runs on review entry, not on approve.** Pre-renders CV + CL to PDF and DOCX when job enters `review`. `approve` only transitions state (no rendering).

## Pipeline specifics

- **Fit-assessment**: always-on pre-check. Agent returns `<<<FINAL>>>` whose first line is `FIT` or `UNFIT`. No `NEED_INPUT` path. UNFIT → modal with Dismiss/Ignore buttons.
- **Crash recovery**: on startup, any job with `state=running` reverts to its last completed stage. `awaiting_input` jobs are untouched.
- **Job identity**: `sha1(f"{company}|{role}|{link}")[:16]`. Re-runs skip `approved` jobs unless JD hash changed.

## Testing

- **Fakes over mocks.** Use `tests/backend/fakes/fake_backend.py` (`FakeAgentBackend`) — never `patch` or `MagicMock` internal functions.
- `FakeRenderer` must write a stub file (e.g. `b"PDF"`) — don't skip the renderer call.
- Integration tests (real API calls) in `tests/backend/test_integration.py`, marked `@pytest.mark.integration`.

## Frontend

- React 18 + Vite + TypeScript, Zustand store, Tailwind CSS.
- Single-page workspace (no router). WS at `/ws` uses native `EventSource`-style fan-out.
- Build output goes to `jsa/static/` — served by FastAPI in production.

## Backend agent pattern

Every backend implements `AgentBackend` ABC:
- `start_session(system_prompt, initial_user_msg) → (SessionHandle, AgentReply)`
- `restore_session(system_prompt, history, external_id) → SessionHandle`
- `send_message(handle, text) → AgentReply`
- `end_session(handle)`
