# JSA — Job Search Assistant

JSA is a CLI-launched local web application that automates tailored job-application document generation. You supply a CSV of job openings and your CV; for each row a two-stage AI pipeline (CV adjustment → cover letter) produces tailored Markdown that you review in a browser UI, request revisions on, and export to PDF.

**Features:**

- Two-stage pipeline per job: CV tailoring + cover letter generation
- Up to 5 jobs processed concurrently with automatic semaphore control
- Follow-up Q&A: the AI asks clarifying questions; you answer them in the browser Inbox
- Full crash resilience: every stage is checkpointed; resume exactly where you left off after any crash or restart
- Revision loop: request edits on generated CV or cover letter; new document version written without re-running the whole pipeline
- Browser UI with live pipeline progress, document preview, and PDF export
- Three AI backends: Claude CLI, Gemini CLI, Anthropic REST API
- All data stored locally in SQLite (`~/.jsa/jsa.sqlite`)

---

## Requirements

- **Python 3.11+**
- **Node.js 18+** (only needed once to build the frontend bundle)
- **System libraries for WeasyPrint:**
  - macOS: `brew install cairo pango gdk-pixbuf libffi`
  - Linux (Debian/Ubuntu): `apt install libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`

---

## Install

```bash
# Clone the repository
git clone https://github.com/yourname/jsa.git
cd jsa

# Install the Python package (editable install includes all dependencies)
pip install -e .

# Build the frontend bundle (required once; re-run after any frontend changes)
cd frontend && npm install && npm run build && cd ..
```

---

## Quick start

```bash
jsa --csv jobs.csv --cv resume.pdf
```

This opens `http://localhost:8765` in your browser, ingests the CSV rows as jobs, and begins processing them in the background. Pipeline progress updates live in the UI.

---

## CSV format

The CSV file must have exactly these five columns (order matters):

| Column    | Description                                      |
|-----------|--------------------------------------------------|
| `company` | Company name                                     |
| `role`    | Role / job title                                 |
| `link`    | URL to the job posting                           |
| `tier`    | Priority tier: `A` (high), `B` (medium), `C` (low) |
| `JD`      | Full job description text (may be multi-line)    |

**Example:**

```csv
company,role,link,tier,JD
Acme Corp,Software Engineer,https://acme.com/jobs/1,A,"We are looking for a software engineer to join our platform team. You will work on distributed systems..."
Beta Inc,Product Manager,https://beta.io/careers/pm,B,"Seeking a PM with 3+ years experience in B2B SaaS. Responsibilities include..."
```

Multi-line job descriptions must be wrapped in double quotes (standard CSV quoting). Most spreadsheet exports handle this automatically.

---

## CLI flags

| Flag | Default | Environment variable | Description |
|------|---------|----------------------|-------------|
| `--csv` | required | — | Path to the jobs CSV file |
| `--cv` | required | — | Path to your CV (`.pdf` or `.docx`) |
| `--out` | `output/` | `JSA_OUTPUT_DIR` | Directory where PDFs are written on approval |
| `--backend` | `claude-cli` | `JSA_BACKEND` | AI backend: `claude-cli` \| `gemini-cli` \| `anthropic` |
| `--db` | `~/.jsa/jsa.sqlite` | `JSA_DB_PATH` | SQLite database path |
| `--port` | `8765` | `JSA_PORT` | Port for the local web server |
| `--no-browser` | false | — | Skip opening the browser automatically |

---

## Backends

### `claude-cli` (default)

Uses the `claude` CLI tool as a subprocess. Requires `claude` to be installed and authenticated.

Download and authenticate: [https://claude.ai/download](https://claude.ai/download)

```bash
jsa --csv jobs.csv --cv resume.pdf --backend claude-cli
```

### `gemini-cli`

Uses the `gemini` CLI tool as a subprocess. Requires `gemini` to be installed.

Setup instructions: [Google AI Studio](https://aistudio.google.com/)

```bash
jsa --csv jobs.csv --cv resume.pdf --backend gemini-cli
```

### `anthropic`

Uses the Anthropic REST API directly. Requires the `ANTHROPIC_API_KEY` environment variable to be set.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
jsa --csv jobs.csv --cv resume.pdf --backend anthropic
```

Optional environment variables for this backend:

| Variable | Default | Description |
|----------|---------|-------------|
| `JSA_MODEL` | `claude-opus-4-7` | Model name to use |
| `JSA_ANTHROPIC_TIMEOUT` | `180` | Per-request timeout in seconds |

---

## Workflow

1. **Start JSA.** Run `jsa --csv jobs.csv --cv resume.pdf`. The browser opens at `http://localhost:8765`.

2. **Pipeline runs in background.** For each job in the CSV, the AI runs two stages:
   - *CV adjustment* — tailors your CV for the specific role and JD
   - *Cover letter* — writes a matching cover letter

3. **Answer follow-up questions.** If the AI needs clarification (e.g., "What sector are you targeting?"), the job parks and a question appears in the **Inbox** tab. Type your answer and submit; the job resumes automatically.

4. **Review documents.** Once both stages complete, the job moves to **Review**. Click the job in the left rail to open both the adjusted CV and cover letter side-by-side. Preview is rendered Markdown — no PDF overhead.

5. **Request revisions (optional).** In the Review pane, use the revision chat box to send targeted instructions (e.g., "Make the skills section shorter"). The AI revises the specific document without re-running the whole pipeline. A new document version is written.

6. **Approve and export PDFs.** When satisfied, click **Approve & Export**. Two PDFs are written to the output directory:
   - `{output_dir}/{company}_{role}_{job_id[:8]}/cv.pdf`
   - `{output_dir}/{company}_{role}_{job_id[:8]}/cover_letter.pdf`

7. **PDFs are in `output/`** (or the path you set with `--out`). The job moves to **Done**.

---

## Prompt customisation

The AI prompts live at:

```
jsa/prompts/PROMPT_CDADJUST.md   # CV adjustment system prompt
jsa/prompts/CVL_PROMPT.md        # Cover letter system prompt
```

Edit these files in any text editor. Changes take effect immediately on the next job run (prompts are read from disk on every invocation, never cached).

**MANDATORY: sentinel format.** Every prompt MUST instruct the model to terminate every reply with exactly one of these two blocks:

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

**Do not remove or modify the sentinel instructions in the prompts.** The pipeline parser (`jsa/agents/protocol.py`) will raise a `ProtocolError` and mark the job `failed` if the sentinel is absent or malformed. The sentinel instructions are already present in the default prompt stubs.

---

## Persistence and re-runs

**Database location:** `~/.jsa/jsa.sqlite` (configurable via `--db`).

**Re-running with the same CSV** resumes from the last checkpoint — no work is repeated:

- Jobs in `approved` state with unchanged JD are skipped entirely.
- Jobs in `failed` state are reset to `pending` and restarted.
- Jobs in `running`, `cv_done`, `awaiting_input`, or `review` resume from where they left off.
- If a job's JD has changed since the last run, it is reset to `pending` and re-processed.

**Crash recovery:** on every startup JSA runs a recovery sweep. Any job stuck in `running` (which indicates it was mid-stage when the process died) is reverted to the last completed stage:
- Has a cover letter document → `cv_done` (cover letter stage will be re-run)
- Has a CV document → `cv_done`
- Has neither → `pending`

Jobs in `awaiting_input` are left untouched — the question is already in the database.

**Reset a failed job:** click the **Reset** button in the job's detail pane in the UI. This transitions the job back to `pending` so it will be re-processed on the next wakeup.

---

## Development

### Backend tests

```bash
pytest -v
```

Skip integration tests (which call real APIs or require full end-to-end setup):

```bash
pytest -v -m "not integration"
```

Run only integration tests:

```bash
pytest tests/backend/test_integration.py -v -m integration
```

### Frontend tests

```bash
cd frontend && npm test
```

### Frontend dev server (hot reload)

```bash
# In one terminal: start the JSA backend
jsa --csv jobs.csv --cv resume.pdf --no-browser

# In another terminal: start the Vite dev server
cd frontend && npm run dev
# Open http://localhost:5173
```

The Vite dev server proxies API calls to `http://localhost:8765` automatically.

### Project structure

```
jsa/                   Python package
  cli.py               Entry point (typer)
  config.py            Settings (pydantic-settings)
  server.py            FastAPI app factory
  db/                  SQLAlchemy models, engine, repository
  ingest/              CSV and CV loaders
  agents/              AgentBackend ABC + three backends
  prompts/             Prompt files (edit these)
  pipeline/            Orchestrator, stage runners, state machine
  render/              PDF renderer (WeasyPrint)
  events/              In-process pub/sub bus
  api/                 FastAPI route handlers
frontend/              React + Vite + TypeScript UI
tests/                 pytest (backend) + vitest (frontend)
```
