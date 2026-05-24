# Architecture: JSA (Job Search Assistant)

## Overview
JSA is a CLI-launched local web application that automates tailored job-application document generation. The user supplies a CSV of openings and their CV; for each row, a two-stage AI pipeline (CV adjustment → cover letter) produces tailored Markdown that the user reviews in a browser GUI and exports to PDF. The system runs up to five jobs concurrently, parks jobs that need follow-up input, and persists all state in SQLite so it survives crashes and API-quota pauses.

Primary design goals, in priority order:
1. **Resilience.** Every pipeline stage is checkpointed; the system can crash mid-run and resume.
2. **Concurrency without blocking.** A parked job frees its worker slot; the next CSV row starts immediately.
3. **Backend portability.** Agent and PDF-renderer logic sits behind small ABCs so backends are swappable.
4. **Local-first.** No deployment, no auth, no multi-user — a single user on a single machine.

## Technology stack
- **Language / runtime:** Python 3.11+ (async/await, `typing.Literal`, structural pattern matching).
- **Backend framework:** FastAPI (async-native) served by `uvicorn`.
- **DB:** SQLite via SQLAlchemy 2.0 (async engine, `aiosqlite` driver).
- **CLI:** `typer` for the `jsa` entry point.
- **Realtime:** native FastAPI WebSocket.
- **Document input:** `python-docx` (DOCX), `pypdf` (PDF text extraction).
- **PDF output:** `weasyprint` (default) behind a `Renderer` ABC. Note: weasyprint requires system libs `cairo`, `pango`, `gdk-pixbuf` — document in the README.
- **Markdown → HTML:** `markdown-it-py` (used by the weasyprint renderer).
- **Subprocess agents:** `ptyprocess` (or `asyncio.create_subprocess_exec` with a pty FD) for Claude/Gemini CLI backends.
- **REST agent:** `anthropic` Python SDK.
- **Frontend:** React 18 + Vite + TypeScript. State via Zustand (lightweight; no Redux). Styling: Tailwind. No router needed (single-page workspace + tabs).
- **Testing:** `pytest` + `pytest-asyncio` for backend; `vitest` + `@testing-library/react` for frontend.
- **Migrations:** `Base.metadata.create_all()` for v1. Note Alembic as future work; do not set it up now.

## Project structure
```
JSA/
├── ARCH.md
├── README.md
├── pyproject.toml                # Python deps, jsa entry point, ruff/pytest config
├── jsa/                          # Python package
│   ├── __init__.py
│   ├── cli.py                    # `jsa` entry point (typer); parses args, launches server, opens browser
│   ├── config.py                 # Settings (pydantic-settings): output dir, backend choice, model, db path
│   ├── server.py                 # FastAPI app factory; mounts routes, WS, static frontend bundle
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py             # async SQLAlchemy engine + session factory
│   │   ├── models.py             # ORM models: Job, Message, Document, FollowUp
│   │   └── repo.py               # Repository functions (get_job, list_jobs, upsert_job, etc.)
│   ├── ingest/
│   │   ├── csv_loader.py         # CSV parsing + (company, role, link) hashing for job IDs
│   │   └── cv_loader.py          # PDF/DOCX → plain-text extraction
│   ├── agents/
│   │   ├── base.py               # AgentBackend ABC, SessionHandle, AgentReply dataclasses
│   │   ├── protocol.py           # Sentinel grammar + parser (NEED_INPUT/FINAL)
│   │   ├── claude_cli.py         # ClaudeCliBackend (pty subprocess)
│   │   ├── gemini_cli.py         # GeminiCliBackend (pty subprocess)
│   │   ├── anthropic_api.py      # AnthropicAPIBackend (REST, multi-turn)
│   │   └── registry.py           # backend_for(name) -> AgentBackend
│   ├── prompts/
│   │   ├── loader.py             # read_prompt(name) -> str (no caching)
│   │   ├── PROMPT_CDADJUST.md    # CV-adjust system prompt (stub)
│   │   └── CVL_PROMPT.md         # Cover-letter system prompt (stub)
│   ├── pipeline/
│   │   ├── orchestrator.py       # Dispatcher: semaphore(5), runnable queue, task spawn
│   │   ├── stages.py             # run_cv_adjust(job), run_cover_letter(job), run_revision(...)
│   │   ├── state_machine.py      # Allowed transitions + guards
│   │   └── checkpoints.py        # Checkpoint write/read helpers
│   ├── render/
│   │   ├── base.py               # Renderer ABC
│   │   ├── weasy.py              # WeasyPrintRenderer (default)
│   │   └── registry.py           # renderer_for(name) -> Renderer
│   ├── events/
│   │   ├── bus.py                # In-process pub/sub (asyncio.Queue per WS client)
│   │   └── schema.py             # Event envelope dataclasses + Literal types
│   ├── api/
│   │   ├── routes_jobs.py        # /api/jobs, /api/jobs/{id}, /api/jobs/{id}/answer, /approve, /revise
│   │   ├── routes_meta.py        # /api/health, /api/config
│   │   └── ws.py                 # /ws — WebSocket fan-out
│   └── static/                   # Built frontend bundle copied here at install/dev
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── index.html
│   └── src/
│       ├── main.tsx
│       ├── App.tsx
│       ├── store.ts              # Zustand store: jobs map, WS connection, active job
│       ├── api.ts                # fetch wrappers
│       ├── ws.ts                 # WebSocket client + event dispatcher
│       └── components/
│           ├── JobList.tsx       # Left rail — all jobs with status
│           ├── Inbox.tsx         # awaiting_input jobs surface here
│           ├── JobDetail.tsx     # Right pane — stage progress, logs
│           ├── ReviewPane.tsx    # Markdown preview of CV + CL, approve/revise buttons
│           ├── ChatBox.tsx       # Follow-up Q/A and revision chat
│           └── StatusBadge.tsx
└── tests/
    ├── backend/                  # pytest
    │   ├── test_csv_loader.py
    │   ├── test_state_machine.py
    │   ├── test_orchestrator.py
    │   ├── test_protocol_parser.py
    │   ├── test_renderer.py
    │   └── fakes/
    │       └── fake_backend.py   # AgentBackend impl for tests
    └── frontend/                 # vitest
        └── ...
```

## Core components

### `jsa.cli`
Entry point. Args: `--csv <path>`, `--cv <path>`, `--out <dir>`, `--backend {claude-cli,gemini-cli,anthropic}`, `--db <path>`, `--no-browser`. Validates inputs, initialises DB (create_all), upserts CSV rows as `Job`s, starts uvicorn in-process, opens browser to `http://localhost:8765`.

### `jsa.config.Settings`
Pydantic settings, populated from CLI flags + env vars. Single source of truth for paths and backend choice. Passed via dependency injection into FastAPI.

### `jsa.db.models`
SQLAlchemy ORM models (see Data model section).

### `jsa.ingest.csv_loader`
- Validates header: `company,role,link,tier,JD`.
- Tier is constrained to `{A,B,C}`; rows with invalid tier are surfaced as soft errors and skipped.
- Job ID = `sha1(f"{company}|{role}|{link}")[:16]`. Stable across re-runs.

### `jsa.ingest.cv_loader`
`load_cv(path: Path) -> str` returns plain text. Dispatches on extension. PDF via `pypdf`, DOCX via `python-docx`. Result cached in `Job.cv_text` (or a separate `CvSource` row keyed by hash).

### `jsa.agents.base`
The single most important abstraction in the system. See Agent abstraction section for the full contract.

### `jsa.agents.protocol`
Defines the sentinel grammar that all prompts MUST emit (see Key design decisions → "Follow-up detection protocol"). Provides `parse_reply(raw: str) -> AgentReply`.

### `jsa.prompts.loader`
```python
def read_prompt(name: Literal["cv_adjust", "cover_letter"]) -> str:
    path = PROMPTS_DIR / {"cv_adjust": "PROMPT_CDADJUST.md", "cover_letter": "CVL_PROMPT.md"}[name]
    return path.read_text()  # no caching — always fresh
```

### `jsa.pipeline.orchestrator`
The dispatcher loop. Owns one `asyncio.Semaphore(5)` and a "runnable" queue. Pulls a runnable job, spawns a task that runs one pipeline stage to completion, releases the slot, repeats. See Concurrency model.

### `jsa.pipeline.stages`
Pure(-ish) functions: each takes a `Job` + a constructed `AgentBackend` and runs exactly one stage end-to-end (including any follow-ups). Writes Markdown output and the new checkpoint atomically before returning.

### `jsa.pipeline.state_machine`
Allowed transitions table + `transition(job, new_state)` guard. Invalid transitions raise; the orchestrator catches and marks `failed`.

### `jsa.render.base`
`Renderer` ABC. Default impl is `WeasyPrintRenderer`. Renderer is invoked **only on approval** — never speculatively.

### `jsa.events.bus`
Tiny in-process pub/sub. WS endpoints register a per-connection `asyncio.Queue`; the bus broadcasts every event to all queues. No persistence on the bus — events are derived from DB state, so a reconnecting client refetches `/api/jobs` and is up to date.

### `jsa.api.*`
Thin routes. All long-running work happens in the orchestrator; routes only mutate DB rows and publish events.

## Data flow

### Initial ingest
1. `jsa --csv jobs.csv --cv resume.pdf` → CLI extracts CV text, hashes each CSV row to a job ID, inserts/updates `Job` rows with `state=pending` (unless `state in {approved}`, which are skipped).
2. CLI starts uvicorn; on startup hook the server runs a **crash-recovery sweep** (see Startup sequence).
3. Orchestrator starts pulling runnable jobs.

### Per-job happy path
```
pending
  → [worker picks up, state=running, stage=cv_adjust]
  → CV-adjust agent session opens with system prompt + (cv_text, jd, tier, optional research notes)
  → agent returns FINAL → markdown stored in Document(stage=cv), state=cv_done
  → [next worker slot, state=running, stage=cover_letter]
  → CL agent session, returns FINAL → Document(stage=cl), state=cl_done
  → state=review (auto)
  → user clicks Approve → renderer produces 2 PDFs → state=approved
```

### Follow-up path (park & resume)
```
running (cv_adjust)
  → agent reply parsed as NEED_INPUT
  → FollowUp row inserted (question, stage)
  → Job.state=awaiting_input, worker task EXITS (slot released)
  → event: follow_up_needed
  → user types answer in Inbox → POST /api/jobs/{id}/answer
  → FollowUp.answer stored; Job re-enqueued (runnable)
  → orchestrator dispatches; new worker reconstructs session by REPLAYING Message history
  → agent receives the answer as the next user turn
  → continues until FINAL or another NEED_INPUT
```

### Review iteration
```
review
  → user types revision request → POST /api/jobs/{id}/revise {target: "cv"|"cl", text}
  → orchestrator spawns a revision task: reopen session for that stage, send revision message
  → new Document version written; state stays `review`
  → user re-reviews; eventually approves
```

## Key design decisions

### Follow-up detection protocol (sentinel-based)
**Decision:** every agent reply MUST end with exactly one of two sentinel blocks, defined in the system prompt and parsed by `agents.protocol`:
```
<<<NEED_INPUT>>>
<question to user>
<<<END>>>
```
or
```
<<<FINAL>>>
<final markdown payload>
<<<END>>>
```
**Why:** pty subprocess CLIs stream free text; we cannot reliably distinguish "asking a question" from "delivering output" without an in-band marker. JSON-only protocols are fragile under partial streaming. Sentinels work uniformly across CLI and REST backends and are enforceable in the system prompt.
**Alternatives considered:** structured JSON every turn (rejected — too easy for CLI agents to break with partial output / markdown fences); tool-call style (rejected — Anthropic-API-only, not portable).
**Implication for prompt authors:** the two prompt files must instruct the model to terminate every reply with exactly one of these two blocks.

**Parser resolution rules** (`agents/protocol.parse_reply`):
1. Scan the raw text for `<<<NEED_INPUT>>>...<<<END>>>` and `<<<FINAL>>>...<<<END>>>` (greedy, multi-line).
2. **Exactly one** block of either kind ⇒ valid; classify by kind, `content` is the inside.
3. **Zero blocks** ⇒ raise `ProtocolError("no sentinel block")`. The stage marks `failed`.
4. **Multiple blocks of any combination** ⇒ take the **last** complete block in the raw text (assumption: agents may "think out loud" before terminating; the terminating block wins). Log a warning event.
5. **Unclosed sentinel** (open without matching `<<<END>>>`) ⇒ raise `ProtocolError("unterminated block")`.
6. **Nested sentinels** are not supported; the parser treats `<<<END>>>` as a literal terminator regardless of nesting depth. Prompt authors must not allow nesting.

### Park-as-task-exit (not coroutine suspension)
**Decision:** when a job parks, the worker task **completes** with the job's state checkpointed to `awaiting_input`. The semaphore slot is released by task completion. When the user answers, the orchestrator enqueues the job again; a **fresh task** picks it up and continues the stage.
**Why:** simpler than long-lived worker objects with park/unpark logic; collapses concurrency to `Semaphore(5)` + a runnable queue + a dispatcher loop. Also crash-safe by construction — there is no in-memory worker state to lose.
**Alternatives considered:** keep the coroutine alive while awaiting user input (rejected — unbounded resident tasks; dies with the FastAPI process anyway).

### Agent-session lifecycle: native restore, not user-turn replay
**Decision:** agent sessions do NOT persist across park. On resume, the orchestrator calls a dedicated **`restore_session(system_prompt, history)`** ABC method (see Agent abstraction) that each backend implements **natively** — preserving the original assistant turns verbatim, not regenerating them.
- `AnthropicAPIBackend`: passes the full `messages` array (system + alternating user/assistant) on the first API call of the resumed turn. The provider treats the prior assistants as authoritative.
- `ClaudeCliBackend`: uses the CLI's `--resume <session-id>` flag with a persisted `session_id` (stored on the `Job` row when the session is first started). The pty is reopened against the same session.
- `GeminiCliBackend`: uses the equivalent resume flag if available; otherwise, see fallback below.
**Why not "re-run user turns and discard assistants"?** Fresh inference during replay produces *different* assistant content than what's in history. If a prior assistant asked a follow-up, the replayed one may not ask the same question, and the user's answer (sent as the next turn) would no longer correspond to a real question. That's a silent corruption mode that "works in tests."
**Fallback for any backend that lacks native resume:** the system prompt MUST make follow-up generation deterministic given the user turns (i.e., the assistant should ask the same follow-up given the same inputs). This is a prompt-engineering containment, not an architectural one, and is acceptable only if a native resume mechanism is unavailable for that backend.
**Open question to resolve in implementation:** verify Gemini CLI exposes a resume mechanism. If not, document the prompt-determinism requirement in `CVL_PROMPT.md` / `PROMPT_CDADJUST.md` headers.
**Why tear-down at all?** Crash resilience: a held-open subprocess dies with the parent. Native restore is the only mechanism that survives both parks and crashes.

### Job identity and re-run semantics
- Primary key: `id = sha1(company|role|link)[:16]`.
- On re-run with the same CSV:
  - Rows already `approved` → skipped.
  - Rows in `failed` → reset to `pending` (restart from scratch).
  - Anything else → resumes from last checkpoint.
- A row whose JD has changed since the last run gets a new `jd_hash`; if the hash differs, the job is **reset to pending** and previously-generated documents are kept in history but no longer current.

### Crash-recovery rule
On server startup: every job with `state == running` is reverted to the last completed stage (`pending` → still pending; if it had a `cv` Document, → `cv_done`; if it had both, → `cl_done`). Jobs in `awaiting_input` stay put — their question is already in the DB. Jobs in `review` / `approved` / `failed` are untouched.

### Renderer is post-approval only
The renderer runs only when the user clicks Approve. Mid-pipeline previews are rendered Markdown in the browser (no PDF generation overhead). This keeps weasyprint's heavy dependencies off the hot path and prevents wasted work on documents that get revised.

### Single-process, single-user
No auth, no multi-user, no remote deployment. Localhost only. The frontend bundle is served statically from FastAPI. CORS is wide-open for `http://localhost:*`.

## Constraints and non-goals
- **Concurrency cap:** exactly 5 simultaneous agent sessions. Hard cap on tokens / network is the user's responsibility (API key quota).
- **Not multi-user.** No accounts, sessions, RBAC.
- **No prompt editing in-app.** Prompts are files on disk; the user edits them in any editor.
- **No live PDF preview.** PDFs are produced on approval, not continuously.
- **No queue beyond runnable + awaiting_input.** No priorities, no scheduling windows, no retries with backoff (failure → `failed`; user must manually reset).
- **No tier-C "skip altogether" yet.** Configurable later; v1 always runs both stages for all tiers.
- **Platform:** macOS + Linux. Windows untested; weasyprint on Windows is painful.

## Testing approach
- **Framework:** `pytest` + `pytest-asyncio` (backend). `vitest` + `@testing-library/react` (frontend).
- **Layout:** mirror source structure under `tests/backend/` and `tests/frontend/`.
- **Fakes over mocks:** a `FakeAgentBackend` in `tests/backend/fakes/fake_backend.py` lets the orchestrator and state machine be tested deterministically — it returns scripted `AgentReply` sequences. A `FakeRenderer` writes a stub file.
- **Coverage targets (informal):**
  - 100% of `state_machine.transition()` paths.
  - 100% of `protocol.parse_reply()` (all sentinel edge cases: malformed, missing END, both blocks present, neither present).
  - End-to-end "happy path" + "park/resume" + "crash recovery" tests against an in-memory SQLite + FakeAgentBackend.
- Real-backend tests (Claude/Gemini CLI, Anthropic API) live under `tests/backend/integration/`, are marked `@pytest.mark.integration`, skipped in CI by default.

## Data model

```python
# jsa/db/models.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Enum as SAEnum
from datetime import datetime
import enum

class Base(DeclarativeBase): ...

class JobState(str, enum.Enum):
    pending = "pending"
    running = "running"
    awaiting_input = "awaiting_input"
    cv_done = "cv_done"
    cl_done = "cl_done"
    review = "review"
    approved = "approved"
    failed = "failed"

class Stage(str, enum.Enum):
    cv_adjust = "cv_adjust"
    cover_letter = "cover_letter"
    revising_cv = "revising_cv"
    revising_cl = "revising_cl"

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # sha1[:16]
    company: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(255))
    link: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(String(1))                   # A | B | C
    jd: Mapped[str] = mapped_column(Text)
    jd_hash: Mapped[str] = mapped_column(String(16))
    cv_text: Mapped[str] = mapped_column(Text)                     # extracted CV text snapshot
    state: Mapped[JobState] = mapped_column(SAEnum(JobState))
    current_stage: Mapped[Stage | None] = mapped_column(SAEnum(Stage), nullable=True)
    session_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # backend resume token
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    follow_ups: Mapped[list["FollowUp"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    revision_requests: Mapped[list["RevisionRequest"]] = relationship(back_populates="job", cascade="all, delete-orphan")

class Message(Base):
    """Full transcript for replay. role in {system, user, assistant}."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="messages")

class Document(Base):
    """Markdown output per stage. Latest version is the highest `version` per (job, stage)."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    version: Mapped[int] = mapped_column(Integer)
    markdown: Mapped[str] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # set on approval
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="documents")

class FollowUp(Base):
    __tablename__ = "follow_ups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[Stage] = mapped_column(SAEnum(Stage))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    asked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    job: Mapped[Job] = relationship(back_populates="follow_ups")

class RevisionRequest(Base):
    """User-requested revision against a Document. Consumed by the orchestrator."""
    __tablename__ = "revision_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    target: Mapped[Stage] = mapped_column(SAEnum(Stage))           # cv_adjust | cover_letter (the doc being revised)
    instruction: Mapped[str] = mapped_column(Text)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[Job] = relationship(back_populates="revision_requests")
```

Indices and invariants:
- Index `Job(state)` (dispatcher scans), `Message(job_id, stage, id)` (replay order), `FollowUp(job_id, answered_at)` (inbox).
- **Invariant: at most one unanswered FollowUp per (job_id, stage) at any time.** Enforced by a unique partial index:
  ```sql
  CREATE UNIQUE INDEX uq_followup_open ON follow_ups(job_id, stage) WHERE answered_at IS NULL;
  ```
  The orchestrator and `runnable_jobs` query both rely on this — a job in `awaiting_input` has exactly one open FollowUp to answer.
- **Invariant: at most one unconsumed RevisionRequest per job.** Unique partial index:
  ```sql
  CREATE UNIQUE INDEX uq_revision_open ON revision_requests(job_id) WHERE consumed_at IS NULL;
  ```
- **Transactional unit.** Every checkpoint commit — Message rows, Document row, Job state/stage update, FollowUp insertion/answer — is written in a **single DB transaction** via `repo.checkpoint(...)`. A crash mid-write leaves the DB in the prior consistent state and the recovery sweep can act on it deterministically.

## API contract

### REST (JSON)
| Method | Path                            | Body / Query                                | Returns                                |
|--------|---------------------------------|---------------------------------------------|----------------------------------------|
| GET    | `/api/health`                   | —                                           | `{ok: true}`                           |
| GET    | `/api/config`                   | —                                           | `{backend, output_dir, ...}`           |
| GET    | `/api/jobs`                     | `?state=`                                   | `Job[]` (summary)                      |
| GET    | `/api/jobs/{id}`                | —                                           | `Job` (full, with documents + messages)|
| POST   | `/api/jobs/{id}/answer`         | `{follow_up_id, text}`                      | `Job` (now in runnable queue)          |
| POST   | `/api/jobs/{id}/approve`        | —                                           | `{cv_pdf_path, cl_pdf_path}`           |
| POST   | `/api/jobs/{id}/revise`         | `{target: "cv"|"cl", text}`                 | `Job` (state still `review`)           |
| POST   | `/api/jobs/{id}/reset`          | —                                           | `Job` (state=`pending`)                |
| GET    | `/api/jobs/{id}/document/{stage}`| `?version=`                                | `{markdown, version}`                  |

### WebSocket
Single endpoint `GET /ws`. Server pushes events; clients send only ping. Envelope:
```ts
type Event =
  | { type: "status_changed"; job_id: string; payload: { from: JobState; to: JobState } }
  | { type: "stage_complete"; job_id: string; payload: { stage: "cv_adjust" | "cover_letter" } }
  | { type: "follow_up_needed"; job_id: string; payload: { follow_up_id: number; question: string; stage: Stage } }
  | { type: "log"; job_id: string; payload: { level: "info"|"warn"|"error"; text: string } }
  | { type: "error"; job_id: string; payload: { message: string } }
  | { type: "approved"; job_id: string; payload: { cv_pdf_path: string; cl_pdf_path: string } };
```
Events are derived from DB writes — the source of truth is the DB. A reconnecting client should refetch `/api/jobs` to resync, then resume listening.

## Agent abstraction

```python
# jsa/agents/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional, Protocol

@dataclass(frozen=True)
class AgentReply:
    raw: str                                    # full text returned by the model
    content: str                                # text inside the sentinel block
    kind: Literal["final", "needs_input"]
    question: Optional[str] = None              # populated iff kind == "needs_input"

class SessionHandle(Protocol):
    """Opaque per-backend handle. Backends may attach process/connection state.
    Backends MUST persist `external_id` (e.g., a Claude CLI session id) so the
    handle can be reconstructed by `restore_session` after a tear-down."""
    id: str
    external_id: str | None  # backend-specific resume token, persisted on Job

@dataclass(frozen=True)
class HistoryTurn:
    role: Literal["user", "assistant"]
    content: str

class AgentBackend(ABC):
    name: str                                   # "claude-cli" | "gemini-cli" | "anthropic"

    @abstractmethod
    async def start_session(
        self,
        system_prompt: str,
        initial_user_msg: str,
    ) -> tuple[SessionHandle, AgentReply]:
        """Open a fresh session. Returns the handle and the agent's first reply."""

    @abstractmethod
    async def restore_session(
        self,
        system_prompt: str,
        history: list[HistoryTurn],
        external_id: str | None,
    ) -> SessionHandle:
        """Reconstruct a previously-ended session WITHOUT generating new assistant
        turns. Implementations:
          - AnthropicAPIBackend: build messages=[...history] and rely on the API
            to treat prior assistants as canonical on the next send_message.
          - ClaudeCliBackend: spawn pty with `--resume <external_id>`.
          - GeminiCliBackend: equivalent resume flag, or fall back to prompt-
            determinism (see ARCH § Agent-session lifecycle).
        Returns a handle ready for `send_message`. Does NOT call the model."""

    @abstractmethod
    async def send_message(self, handle: SessionHandle, text: str) -> AgentReply: ...

    @abstractmethod
    async def end_session(self, handle: SessionHandle) -> None: ...
```

Backend contract notes:
- Every `send_message` MUST return an `AgentReply` whose `kind` is determined by `protocol.parse_reply`. Backends do not interpret content — they only ferry bytes and apply the parser.
- `start_session` MUST populate `SessionHandle.external_id` if the backend supports native resume; the orchestrator persists this on `Job.session_external_id` before the worker exits to park.
- A backend MAY enforce a per-reply timeout; on timeout it raises `AgentTimeout` and the stage marks `failed`.
- CLI backends own the pty lifecycle; `end_session` must SIGTERM and reap the child.
- The orchestrator's resume path is exactly: load `history` from `Message` rows for the current stage → `restore_session(system_prompt, history, job.session_external_id)` → `send_message(handle, user_answer)`. No assistant turns are regenerated.

## Concurrency model

```python
# jsa/pipeline/orchestrator.py — sketch
class Orchestrator:
    def __init__(self, db, backend_factory, max_parallel: int = 5):
        self.sem = asyncio.Semaphore(max_parallel)
        self.wakeup = asyncio.Event()
        self.db = db
        self.backend_factory = backend_factory
        self._stopping = False

    def notify(self):
        self.wakeup.set()

    async def run(self):
        while not self._stopping:
            self.wakeup.clear()
            runnable = await self.db.list_runnable_jobs()  # state in {pending, cv_done with no cl} etc.
            for job in runnable:
                await self.sem.acquire()                   # blocks at 5 in-flight
                asyncio.create_task(self._run_one(job))    # fire-and-forget; releases sem in finally
            await self.wakeup.wait()                       # park until something changes

    async def _run_one(self, job):
        try:
            backend = self.backend_factory()
            stage = next_stage_for(job)                    # cv_adjust | cover_letter
            await stages.run_stage(job, backend, stage)    # writes checkpoints; may parking via raise PausedForInput
        except stages.PausedForInput:
            pass                                           # job is now `awaiting_input` in DB
        except Exception as e:
            await self.db.mark_failed(job.id, str(e))
        finally:
            self.sem.release()
            self.notify()                                  # let the loop pick up next job

    # Called by API routes after an answer is recorded or a revision posted.
    async def kick(self):
        self.notify()
```

Runnable definition (function `list_runnable_jobs`):
- `state == pending` → run `cv_adjust` (fresh `start_session`).
- `state == cv_done` → run `cover_letter` (fresh `start_session`).
- `state == awaiting_input` AND the single open `FollowUp` for the job has `answered_at IS NOT NULL` → resume via `restore_session` for `current_stage`, then `send_message(answer)`.
- `state == review` AND an unconsumed `RevisionRequest` exists → set `current_stage` to `revising_cv`/`revising_cl`, transition to `running`, run revision via `restore_session` of the original stage's session and `send_message(instruction)`. On completion: write new `Document` version, clear `current_stage`, mark `RevisionRequest.consumed_at`, transition back to `review`.

Ordering: FIFO by `Job.updated_at`. No priorities in v1.

## Renderer interface

```python
# jsa/render/base.py
from abc import ABC, abstractmethod
from pathlib import Path

class Renderer(ABC):
    name: str

    @abstractmethod
    async def render(self, markdown: str, output_path: Path) -> None: ...
```

Default impl `WeasyPrintRenderer`:
1. Convert Markdown → HTML via `markdown-it-py` (enable tables, strikethrough).
2. Wrap in a minimal HTML shell with embedded CSS (`jsa/render/styles.css` — letter-size, 1in margin, system font stack).
3. Pass to `weasyprint.HTML(string=...).write_pdf(output_path)` in a thread (`asyncio.to_thread`) — weasyprint is sync and CPU-bound.

Output naming: `{output_dir}/{company}_{role}_{job_id}/CV.pdf` and `..._cover_letter.pdf`. Slugged.

## Frontend structure

Component tree:
```
<App>
  ├── <Header />                         model/backend indicator, total counts
  ├── <main>
  │   ├── <JobList />                    left rail; groups: Inbox, Running, Review, Done, Failed
  │   ├── <JobDetail>                    right pane; switches by selected job
  │   │   ├── <StatusBadge />
  │   │   ├── <StageTimeline />         pending → cv → cl → review → approved
  │   │   ├── <ReviewPane />            visible when state ∈ {review, approved}
  │   │   │   ├── <MarkdownPreview kind="cv" />
  │   │   │   ├── <MarkdownPreview kind="cl" />
  │   │   │   ├── <ApproveButton />
  │   │   │   └── <ChatBox kind="revise" />
  │   │   ├── <FollowUpPane />          visible when state == awaiting_input
  │   │   │   └── <ChatBox kind="answer" />
  │   │   └── <LogTail />               recent WS log events for this job
```

State (Zustand):
```ts
interface Store {
  jobs: Record<string, JobDTO>;
  selectedId?: string;
  wsStatus: "connecting" | "open" | "closed";
  upsertJob(j: JobDTO): void;
  applyEvent(e: Event): void;
  refetchAll(): Promise<void>;
}
```

WS dispatcher (`ws.ts`) translates each event to a `store.applyEvent(e)` call. On reconnect, the dispatcher triggers `refetchAll()` first to resync, then resumes streaming.

## State machine

The `JobState` enum is 1D; the *active stage* is carried separately in `Job.current_stage`. The pair `(state, current_stage)` is the true discriminator the orchestrator dispatches on. Revisions stay in `review` and use `current_stage ∈ {revising_cv, revising_cl}` to signal there is outstanding work; the runnable definition picks them up via that field, not via a state change to `running`.

```
pending ──► running(cv_adjust) ──┬──► cv_done ──► running(cover_letter) ──┬──► cl_done ──► review ◄──► review
                                  │                                       │                  │
                                  ▼                                       ▼                  ▼
                            awaiting_input                          awaiting_input      awaiting_input
                                  │                                       │                  │  (during revision)
                                  └──► running(cv_adjust) ─► (cv_done)    └──► running(cover_letter) ─► (cl_done)
                                                                                              │
                                                                                              ▼
                                                                                           approved

Any non-terminal state ──► failed (caught exception)
failed ──► pending (via /api/jobs/{id}/reset)
approved is terminal.
```

Revision flow detail: when the user POSTs to `/api/jobs/{id}/revise` with `target: "cv"|"cl"`, the API sets `current_stage = revising_cv` or `revising_cl` and inserts a `RevisionRequest` row (table below). The state remains `review`. The orchestrator's runnable query picks it up, runs the revision (possibly parking to `awaiting_input` with `current_stage` preserved), and on completion clears `current_stage` back to `None` (or to the prior value) — state stays `review` throughout.

Allowed transitions table (`pipeline/state_machine.ALLOWED`):
```python
ALLOWED = {
    JobState.pending:        {JobState.running, JobState.failed},
    JobState.running:        {JobState.awaiting_input, JobState.cv_done, JobState.cl_done, JobState.review, JobState.failed},
    JobState.awaiting_input: {JobState.running, JobState.review, JobState.failed},   # review = revision park-and-resume completing
    JobState.cv_done:        {JobState.running, JobState.failed},
    JobState.cl_done:        {JobState.review, JobState.failed},
    JobState.review:         {JobState.running, JobState.awaiting_input, JobState.approved, JobState.failed},
    JobState.approved:       set(),                                                   # terminal
    JobState.failed:         {JobState.pending},                                       # via reset
}
```

Notes on the table:
- `running` is parameterised by `current_stage`. The transition guard validates the pair: e.g., `(running, cv_adjust) → cv_done` is allowed, `(running, cover_letter) → cv_done` is not.
- During revisions the `running` state has `current_stage ∈ {revising_cv, revising_cl}` and may transition to `awaiting_input` (still in revision) or back to `review` (revision complete).
- `transition(job, new_state, new_stage=None)` checks both the state table and the stage compatibility, raising `InvalidTransition` otherwise.

`Stage` enum gains two values for revisions:
```python
class Stage(str, enum.Enum):
    cv_adjust = "cv_adjust"
    cover_letter = "cover_letter"
    revising_cv = "revising_cv"
    revising_cl = "revising_cl"
```

## Startup sequence

When `jsa --csv jobs.csv --cv resume.pdf` is invoked:

1. **CLI parses args** (typer) and constructs a `Settings`. Verifies CSV + CV exist.
2. **DB bootstrap.** Connect to SQLite at `--db` (default `~/.jsa/jsa.sqlite`). Run `Base.metadata.create_all`.
3. **CV extract.** `cv_loader.load_cv(cv_path)` → plain text. Hash it; store under the CV path's basename.
4. **CSV ingest.** For each row:
   - Compute `job_id` and `jd_hash`.
   - If a `Job` with that id exists:
     - If `state == approved` and `jd_hash` unchanged → skip.
     - If `jd_hash` changed → reset to `pending`, clear `current_stage`, archive existing documents.
     - Otherwise leave intact (will be picked up by recovery sweep).
   - Else insert new `Job(state=pending)`.
5. **Crash-recovery sweep.** For every job with `state == running`:
   - If has a `Document(stage=cover_letter)` → set `cl_done`.
   - Else if has a `Document(stage=cv_adjust)` → set `cv_done`.
   - Else → `pending`.
   Jobs in `awaiting_input` are untouched.
6. **Start uvicorn** in-process on `localhost:8765` (port configurable). Mount API + WS + static frontend.
7. **Start orchestrator** as an asyncio task on FastAPI's startup event.
8. **Open browser** to `http://localhost:8765` unless `--no-browser`.
9. **Shutdown.** Ctrl-C triggers FastAPI shutdown → orchestrator sets `_stopping`, waits for in-flight tasks (with a 30s grace), closes DB. Process exits.

## Open questions
- **Exit lifecycle.** Should `jsa` exit automatically once all jobs are `approved` or `failed`, or run indefinitely until Ctrl-C / UI shutdown button? Defaulting to "run until Ctrl-C" for v1; revisit if annoying.
- **Tier C "skip altogether" mode.** Brief mentions it as configurable. Deferring to v1.1; tier C currently runs the same two-stage pipeline as A/B with the JD-only prompt variant.
- **Multiple CVs per run.** Single CV per invocation is assumed. If a user wants per-job CVs later, the schema already supports it (`Job.cv_text` is per-job), but the CLI does not expose it.
- **Concurrent CLI backend processes.** Whether 5 concurrent Claude/Gemini CLI subprocesses share rate limits in a way that breaks parallelism — to be measured empirically. If it bites, lower the cap via config; no architectural change needed.

## Change log
2026-05-23 — Initial architecture document. Establishes Python + FastAPI backend, React + Vite frontend, SQLite persistence, sentinel-based follow-up protocol, park-as-task-exit concurrency, post-approval-only PDF rendering.
2026-05-24 — Phase 4 implementation decision: `SessionHandle` is implemented as a concrete `@dataclass` base class (not a `Protocol`). Backends subclass it and attach process/connection state as additional fields. `FakeSessionHandle` subclasses it for tests. This diverges from the `Protocol` annotation in the spec above; `Protocol` style is left in the conceptual description for documentation clarity but is not the runtime type. All backends in Phases 5 and 11 must subclass `SessionHandle`.
2026-05-23 — Revision-flow correctness: replaced "tear-down + replay user turns" with native `restore_session` ABC method (Anthropic = full messages array, Claude CLI = `--resume`, Gemini CLI = native or prompt-determinism fallback). Reasoning: replaying user turns and discarding regenerated assistant turns silently diverges from prior conversation. Added `session_external_id` to `Job`. Added `RevisionRequest` table and routed revisions through `current_stage ∈ {revising_cv, revising_cl}` with state remaining `review` (closing the state-machine hole where `running` had no path back to `review`). Added `Stage.revising_cv` / `Stage.revising_cl`. Updated allowed-transitions table accordingly. Added sentinel-parser resolution rules (zero/multiple/unclosed/nested cases). Added unique partial indexes enforcing "at most one open FollowUp per (job, stage)" and "at most one unconsumed RevisionRequest per job". Specified the per-checkpoint single-transaction boundary.
