---
name: project-jsa-phase8-prep
description: Phase 8 pre-flight notes — settings/CLI duplication, startup sequence order, static bundle, CORS policy
metadata:
  type: project
---

## config.py and cli.py have duplicated default values — Phase 8 must reconcile

Default values for port, db_path, output_dir, backend etc. are currently defined in both `jsa/config.py` (pydantic-settings) and `jsa/cli.py` (typer flag defaults). Phase 8 updates `cli.py` to do the full startup sequence — this is the moment to make `cli.py` read from `Settings` as the single source of truth and remove the duplicates.

**Why:** Noted in PLAN.md Phase 1 change log: "Default values are duplicated between `config.py` and `cli.py` — Phase 8 should reconcile."

**How to apply:** In Phase 8, have `cli.py` construct a `Settings` object first, then pass it everywhere. Typer flags should override Settings values (not define their own defaults independently).

---

## The startup sequence order matters for crash recovery

Per ARCH.md "Startup sequence":
1. DB bootstrap (create_all)
2. CV extract
3. CSV ingest (upsert jobs)
4. **Crash-recovery sweep** (AFTER ingest, not before)
5. Start orchestrator (as asyncio task on FastAPI startup event)
6. Start uvicorn
7. Open browser

The crash-recovery sweep must run AFTER ingest so that newly-inserted jobs (state=pending) are already in the DB before the sweep touches running jobs. The orchestrator starts as a FastAPI startup hook, not in the CLI directly.

**How to apply:** When implementing Phase 8 cli.py, follow this exact order. The sweep is a one-time function that iterates running jobs and resets them to their last safe state based on Document row existence.

---

## Frontend bundle lives at jsa/static/ — must be copied there before serve

`server.py` serves the static frontend from `jsa/static/`. During development the frontend runs on its own Vite dev server (port 5173). For production use, `npm run build` output goes into `frontend/dist/` and must be copied to `jsa/static/`. Phase 8 doesn't need to automate this copy — document it in README. During Phase 8 dev/testing, use `--no-browser` and test API endpoints directly.

---

## CORS is intentionally wide-open for localhost

```python
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:*"], ...)
```
This is intentional — single-user local tool, no auth, no deployment. Do not tighten CORS.

---

## Phase 8 is the full-system unlock

After Phase 8 completes, `jsa --csv jobs.csv --cv resume.pdf --no-browser` will:
- Bootstrap DB
- Load CV + ingest CSV rows
- Run crash-recovery sweep
- Start uvicorn + orchestrator
- Process jobs (even without the UI — API endpoints are live)

This is the first time the user can do a real end-to-end run. Recommend manual smoke-testing with a real CSV + CV after Phase 8.
