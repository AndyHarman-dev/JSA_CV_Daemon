# Plan: BF-18–21 — Limit Detection, Backend Switching, DOCX Export

## Goal
Three connected improvements: (1) detect backend usage-limit errors across all backends and surface
a clear message instead of the misleading "no sentinel block" ProtocolError; (2) support an ordered
backend fallback chain so jobs automatically continue on the next backend when the active one hits
its limit, with a stage-boundary restart for CLI→CLI switches and a frontend indicator; (3) add
ATS-friendly DOCX export via python-docx alongside the existing PDF, with a frontend toggle and a
re-runnable export endpoint for approved jobs.

## Architecture reference
See ARCH.md — new `AgentLimitReached` exception in `agents/base.py`; `Settings.backends` list
replaces `Settings.backend`; `Job.backend_name` column tracks per-job active backend; `run_stage`
loops over the chain on limit; `DocxRenderer` + export endpoint decouple rendering from approval.

## Phases

- [x] Phase BF-18: `AgentLimitReached` + per-backend detection — Add `AgentLimitReached(RuntimeError)` to `agents/base.py` next to `AgentTimeout`. In `ClaudeCliBackend._parse_with_nudge`: if `ProtocolError("no sentinel block")` fires AND raw output contains limit-indicator keywords (e.g. "usage limit", "rate limit", "limit reached", "quota"), skip the nudge and raise `AgentLimitReached` immediately with the raw output snippet. Do the same in `GeminiCliBackend._parse_with_nudge`. In `AnthropicAPIBackend._call_api`: catch `anthropic.RateLimitError` (HTTP 429) and raise `AgentLimitReached`. Orchestrator (`_run_one`) already catches generic `Exception` and marks failed — add a specific branch before that for `AgentLimitReached` that marks the job failed with a clear human-readable message ("Backend limit reached — switch backends or wait for quota reset"). No state-machine or schema changes in this phase.

- [~] Phase BF-19: Backend chain + per-job backend + mid-stage switch — (a) Config: add `backends: list[str]` to `Settings`, validated against the registry; `--backends claude-cli,gemini-cli` (comma-separated); keep `--backend` as an alias that sets a single-item list for backward compat. (b) Schema: add `backend_name: str | None` column to `Job`; write a new Alembic migration. (c) Orchestrator: `_backend_factory` becomes per-job — read `job.backend_name` (default to `settings.backends[0]`); pass the full chain to `run_stage` or handle chain advancement in `_run_one`. (d) Switch logic: when `run_stage` raises `AgentLimitReached`, `_run_one` checks if a next backend exists in the chain; if yes: persist `job.backend_name = next_backend`, reset job state to the start of the failed stage (`running → pending` for cv_adjust, `running → cv_done` for cover_letter, `running → review` for revisions — add these transitions to the state machine's ALLOWED table), clear `job.session_external_id` and the relevant per-stage session ID (`cv_session_id` or `cl_session_id`) so the next dispatch starts fresh, emit `BackendSwitchedEvent(job_id, from_backend, to_backend)`, and kick the loop; if chain exhausted: fall through to the existing mark-failed path. (e) Fresh-session limit hits (no prior Message rows for this stage): same path — the state reset naturally causes a fresh start_session on the new backend. (f) Frontend: handle `BackendSwitchedEvent` in the WS event loop; display as a prominent log entry "⚡ Switched backend: claude-cli → gemini-cli" in the job's activity log.

- [x] Phase BF-20: `DocxRenderer` + `docx_path` column — Add `python-docx` to `pyproject.toml` dependencies. Write `jsa/render/docx_render.py` with `DocxRenderer(Renderer)`: a manual Markdown-to-DOCX converter using python-docx, ATS-friendly, targeting ≤2 pages. Format rules: 0.75in margins (all sides), 10.5pt Calibri body, 1.3 line spacing, bold section headings (12pt), name as heading (14pt, centered), contact line below name, `---` separators between sections rendered as paragraph borders or thin rules, bullet points for experience items, no decorative elements. Wrap the sync python-docx calls in `asyncio.to_thread` per the concurrency rule. Register as `"docx"` in `render/registry.py`. Add `docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)` to the `Document` model; write an Alembic migration.

- [x] Phase BF-21: Export endpoint + frontend toggle — (a) Backend: add `POST /api/jobs/{id}/export` route with JSON body `{"format": "pdf"|"docx"}`. Requires `job.state == approved`. Runs the requested renderer, writes the file to the same slug directory as approve (`output_dir/slug/cv.{ext}` and `cover_letter.{ext}`), updates `cv_doc.pdf_path`/`docx_path` or `cl_doc.pdf_path`/`docx_path` accordingly, and returns `{"cv_path": "...", "cl_path": "..."}`. Re-runnable: calling it again with the same format overwrites the file. The existing `approve` endpoint continues to render PDF immediately on approval (backward-compatible). (b) Frontend: in the approved-state job detail panel, replace the static PDF download links with a format toggle (PDF / DOCX) and an "Export & Download" button that calls the export endpoint and then triggers a browser download. Show both download links once each format has been generated. Disable the button while export is in progress.

## Constraints
- `AgentLimitReached` lives in `agents/base.py` — backend-agnostic, not prefixed with a backend name.
- Mid-stage CLI→CLI switches use stage-boundary restart (not history fold). The current turn's partial work is lost but the job resumes from the last clean checkpoint on the new backend.
- `run_stage` catch location: `AgentLimitReached` is caught in `_run_one` (orchestrator), not inside `run_stage` itself. `run_stage` propagates it up transparently.
- State machine additions (`running → pending`, `running → cv_done`, `running → review`) are used exclusively for backend-switch restarts; document this in the state machine comments.
- `DocxRenderer` is sync/CPU-bound — always wrapped in `asyncio.to_thread`.
- Renderer is never called speculatively or for preview (CLAUDE.md rule) — only from `approve` and the new `export` endpoint.
- `python-docx` only (no pandoc/pypandoc system dependency).
- BF-20 and BF-21 are independent of BF-18/19 — can be implemented in parallel if desired.

## Open questions
- None. All design decisions resolved.

## Change log
2026-05-31 — Initial plan. BF-18 (limit detection) → BF-19 (backend chain + switch) → BF-20 (DocxRenderer) → BF-21 (export endpoint + frontend toggle).
