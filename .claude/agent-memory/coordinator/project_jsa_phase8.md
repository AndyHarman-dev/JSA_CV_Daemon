---
name: project-jsa-phase8
description: Phase 8 implementation decisions and gotchas — FastAPI backend, event bus, REST routes, CLI startup
metadata:
  type: project
---

## state_machine.py gained two new capabilities in Phase 8

1. `running → pending` added to ALLOWED — needed by `recovery_sweep` for crash-recovery startup.
2. `set_current_stage(job, stage)` function — sets `current_stage` on a `review`-state job without changing state. Only accepts `revising_cv` / `revising_cl`. Used by the revise route and any future code that prepares a revision.

**Why:** `transition()` only sets current_stage when transitioning to `running` or `awaiting_input`. The revision flow keeps state=`review` while changing stage — a pattern not representable through `transition()` alone.

---

## CORS: use allow_origin_regex, not allow_origins wildcard

```python
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    ...
)
```
`allow_origins=["http://localhost:*"]` is NOT valid CORS syntax. The regex approach handles all localhost ports including the Vite dev server (5173) and the app server (8765).

---

## ASGITransport does NOT trigger @app.on_event("startup")

Test fixtures must manually drive lifespan via `app.router.lifespan_context(app)` as an outer async context manager around `AsyncClient`. The startup hook sets `app.state.session_factory` and `app.state.orchestrator` — routes will raise `AttributeError` without it.

**Pattern:**
```python
async with test_app.router.lifespan_context(test_app):
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
        yield ac
```

---

## @app.on_event is deprecated — migrate to lifespan in future phase

FastAPI 0.93+ prefers the `lifespan` context manager. The current `@app.on_event("startup")` / `("shutdown")` pattern still works but will emit deprecation warnings. Migrate before Phase 9 if time permits; it does not break tests.

---

## recover_sweep and answer_follow_up in repo.py

- `recovery_sweep(session)` — iterates running jobs, checks Document rows, transitions to cl_done/cv_done/pending in a single commit. Does NOT touch awaiting_input jobs.
- `answer_follow_up(session, follow_up_id, text)` — sets `fu.answer` + `fu.answered_at`, self-commits. The /answer route calls this then re-fetches in a fresh session.

---

## CLI startup sequence (ARCH.md compliant)

`_preflight()` runs via `asyncio.run()` BEFORE uvicorn starts:
1. `init_db(engine)` — create_all
2. `asyncio.to_thread(load_cv, cv_path)` — CV text extraction (blocking)
3. `load_csv` + `upsert_job` per row — CSV ingest
4. `recovery_sweep()` — crash recovery

Orchestrator starts as asyncio task inside FastAPI's startup hook (not in CLI directly). Double `init_db` call is harmless (create_all is idempotent).
