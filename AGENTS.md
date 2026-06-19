# AGENTS.md — JSA Project

## Implementation Workflow

Whenever asked to implement a feature, a plan phase, or any multi-step task:

1. **Draft a todo list first.** Before writing any code, create a file at `./.opencode/todos/<feature-slug>.md` listing every concrete step as a checkbox. Example path: `./.opencode/todos/excel-table-migration.md`.
2. **Work through the list.** After completing each step, tick its checkbox (`- [x]`) by editing the file, then continue to the next item. Keep the file as a live checklist throughout the session so the current state is always visible.
3. **Clean up on approval.** Once the user confirms the implementation is accepted, delete the todo file with `rm ./.opencode/todos/<feature-slug>.md`.

If a session is interrupted before approval, leave the todo file in place so the next session can resume from where it left off.

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

# Git Branch Policy

This policy applies to every agent and every session, for any type of work (features, fixes, refactors, experiments, etc.).

1. **Create a branch first.** Before writing any code or making any file changes, create a new git branch from the current base branch:
   ```
   git checkout -b <type>/<short-slug>
   ```
   Use a prefix that matches the work: `feat/`, `fix/`, `refactor/`, `chore/`, `experiment/`.

2. **All work stays on that branch.** Never commit directly to `master`, `main`, `develop`, or any other shared branch. Every change, including intermediate commits, goes to the feature branch.

3. **Do not merge without explicit user approval.** When the implementation is complete, present the branch name and a summary of changes, then ask the user whether to merge. Wait for an explicit "yes, merge it" (or equivalent) before running any merge or PR command.

4. **If the user declines or redirects**, keep the branch as-is and note its name so work can be resumed or discarded later.

5. **At the end of your work, mention which branch you branched from**