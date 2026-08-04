# Writing Python tests in JSA

A working brief. Read this before adding a test file; it exists because the existing
suite looks more complicated than it is.

---

## Why the existing files look complex

**There is no `conftest.py` in this repo.** Not at the root, not in `tests/`, not in
`tests/backend/`. Every test file that needs a database re-declares the same engine
fixture from scratch.

`tests/backend/test_db_repo.py::session_factory` and `tests/backend/test_api.py::mem_session_factory`
are the same fixture, copy-pasted. That "engines and whatnot" boilerplate is not
inherent complexity — it is duplication you are expected to copy too.

So: pick the recipe below that matches what you're testing, paste it at the top of your
new file, and move on. You are not missing a shared helper; there isn't one.

---

## Recipe A — pure DB test (no HTTP)

Use when you're testing `jsa.db.repo`, the state machine, or anything that needs a
`session` but not a running app.

```python
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from jsa.db.models import Base


@pytest.fixture
async def session_factory():
    """In-memory SQLite with StaticPool — shared across sessions in one test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s
```

`StaticPool` is **not** optional. Without it, each connection to `:memory:` gets its own
separate database, and a row committed in one session is invisible to the next.

---

## Recipe B — HTTP test against `jsa/server.py`

This is the one you want for `server.py`. Three fixtures, and they must chain in this
order.

```python
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport

from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    from tests.backend.fakes.fake_backend import FakeAgentBackend

    async def _noop(self):
        return

    # Stop the orchestrator's background polling loop from running during tests.
    monkeypatch.setattr(Orchestrator, "run", _noop)
    # No real agent subprocesses / API calls.
    monkeypatch.setattr("jsa.server.backend_for", lambda name, **kw: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backends=["claude-cli"],
        port=8765,
        no_browser=True,
    )
    return create_app(settings)


@pytest.fixture
async def client(test_app):
    # ASGITransport does NOT fire FastAPI's startup handler. Enter the lifespan
    # context manually or app.state.session_factory will not exist.
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.fixture
async def db(test_app, client):
    """session_factory for seeding rows. Depends on `client` so startup has already run."""
    return test_app.state.session_factory
```

Then tests are ordinary:

```python
class TestMetaRoutes:
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
```

Three things in Recipe B that will cost you an hour if you skip them:

1. **The lifespan context.** `ASGITransport` bypasses the ASGI lifespan protocol, so
   `@app.on_event("startup")` never fires on its own. `create_app` builds the engine,
   `session_factory`, `backend_factory`, and `orchestrator` *inside* that startup hook.
   Without `lifespan_context`, `app.state.session_factory` raises `AttributeError`.
2. **`db` must depend on `client`.** Not on `test_app`. Requesting `test_app.state.session_factory`
   before the lifespan has run gets you nothing.
3. **Patch `Orchestrator.run` to a no-op.** Startup does `asyncio.create_task(orchestrator.run())`.
   Leave it live and you get a background poller racing your assertions.

### Use `backends=`, not `backend=`

`Settings` (`jsa/config.py`) carries **both** fields:

- `backends: List[str]` — the ordered fallback chain. This is what `create_app`'s startup
  hook actually passes to the `Orchestrator`.
- `backend: str` — a backward-compat alias, only echoed back by `GET /api/config`
  (`jsa/api/routes_meta.py`). Nothing in the pipeline reads it.

The existing `test_api.py` fixture sets `backend="claude-cli"` and lets `backends` default.
That works by accident. In new tests set `backends=["claude-cli"]`; set `backend=` as well
only if you're asserting on `/api/config`'s payload.

Note that `backends` is validated against `jsa/agents/registry.py::_REGISTRY` at
construction time, so a typo raises a `ValidationError` in the fixture, not later.

### Always pass `db_path=tmp_path / "test.sqlite"`

`Settings` derives two more paths from `db_path` as properties:

```python
cv_structure_path = db_path.parent / "cv_structure.json"
preferences_path  = db_path.parent / "preferences.json"
```

If you construct a bare `Settings()` in a test, `db_path` defaults to
`~/.jsa/jsa.sqlite` — and both of those then point at **your real home directory**. A
test that builds an `Orchestrator` or hits a route will read (or write) live user state
and pass or fail depending on what's on the machine. `Orchestrator.run()` gates on
`cv_structure_path.exists()`, so this shows up as a test that mysteriously passes for you
and hangs in CI, or vice versa.

Setting `db_path` under `tmp_path` isolates all three at once. Do it even when you think
the test doesn't touch the filesystem.

---

## Gotchas

### Patch where the name is *used*, not where it's defined

`jsa/server.py` does `from jsa.agents.registry import backend_for` at module level, binding
the name into the `jsa.server` namespace. So:

```python
monkeypatch.setattr("jsa.server.backend_for", ...)          # works
monkeypatch.setattr("jsa.agents.registry.backend_for", ...) # silently does nothing
```

Same rule for `"jsa.api.routes_jobs.renderer_for"` in the approve tests. This kind of
mistake doesn't error — the test just exercises the real code path and hangs or hits the
network.

### The event bus is a module-level singleton

`jsa/server.py` imports `bus` from `jsa.events.bus` and assigns it to `app.state.bus`.
It is **not** rebuilt per app instance. Anything you `bus.subscribe()` leaks a queue into
every later test in the session.

For bus assertions, construct your own instead:

```python
from jsa.events.bus import EventBus

bus = EventBus()
q = bus.subscribe()
await bus.publish({"type": "log", "job_id": "xyz"})
assert q.get_nowait() == {"type": "log", "job_id": "xyz"}
```

That's what `TestEventBus` in `test_api.py` does, and why.

### No `@pytest.mark.asyncio` needed

`pyproject.toml` sets `asyncio_mode = "auto"`. Write `async def test_...` and async
fixtures directly — no decorator, no `event_loop` fixture.

### Run pytest from the venv

`/opt/homebrew/bin/pytest` is on `PATH` and is **not** the right interpreter — it will
fail with import errors. Use:

```bash
.venv/bin/pytest tests/backend/test_api.py -q
```

---

## Fakes, not mocks

Project convention (see `CLAUDE.md`): do not `patch` or `MagicMock` internal functions.
Use the prebuilt fakes.

| Fake | Path | What it does |
|---|---|---|
| `FakeAgentBackend` | `tests/backend/fakes/fake_backend.py` | Scripted `AgentBackend`. Constructed with a list of `AgentReply`; each `start_session` / `send_message` pops the next. Raises `IndexError` when the script runs dry. |
| `FakeRenderer` | `tests/backend/fakes/fake_renderer.py` | Writes `b"PDF"` to `output_path`, creates parents, records every call in `.calls` as `RenderCall(markdown, output_path)`. |
| FINAL factories | `tests/backend/fakes/finals.py` | `cv_final(marker=...)`, `cl_final(...)` — schema-valid `<<<FINAL>>>` JSON payloads for scripting `FakeAgentBackend`. Use these rather than hand-rolling JSON; the stages run real schema validation. |

`from tests.backend.fakes.fake_backend import FakeAgentBackend` works because `tests/`
and `tests/backend/` both have `__init__.py`. Keep that up if you add a subpackage.

---

## File and naming conventions

- One file per area or bugfix: `test_<area>.py`, or `test_bf<N>_<slug>.py` for a
  regression pinning a specific bug.
- Group with plain classes — `class TestApproveJob:` — no `unittest.TestCase`, no `__init__`.
- Module docstring listing what's covered; `from __future__ import annotations` at the top.
- Section banners (`# ===== Job routes =====`) are used throughout; match them.
- Local helpers prefixed `_`: `_job_data(**overrides) -> dict`, `_insert_job(session_factory, *, state=...)`.
  Copy these from `test_api.py` rather than inventing new shapes.
- Ruff: `line-length = 100`, `target-version = "py311"`.

---

## Running

```bash
.venv/bin/pytest -q                              # everything
.venv/bin/pytest tests/backend/test_api.py -v    # one file
.venv/bin/pytest -q -m "not integration"         # skip real-API tests
```

Integration tests (`tests/backend/integration/`) are marked `@pytest.mark.integration`
and hit real Claude/Gemini/Anthropic APIs. Deselect them by default.

**Get a baseline before you start.** The suite has known pre-existing failures. As of
this writing `tests/backend/test_api.py` is **33 passed, 1 failed** on a clean tree
(`TestApproveJob::test_approve_with_documents_calls_renderer` — `assert 0 == 2`). Run the
file once before adding to it, so a pre-existing failure doesn't read as yours.

Repo-wide the count is considerably higher — other files carry their own known failures.
Don't judge your change against a green full-suite run; judge it against the delta in the
file you touched.

---

## If you're adding `test_server.py`

Scope note: most route behaviour already lives in `test_api.py`. What is genuinely
untested at the `server.py` level, and worth a new file:

- `make_backend_factory(settings)` — its per-name branches (`anthropic`, `claude-cli`, and
  the fall-through default; plus `fit-assessment` once that work lands). A pure function:
  no app, no DB, no fixtures. Cheapest thing to test in the whole module, and currently
  uncovered:

  ```python
  from jsa.config import Settings
  from jsa.server import make_backend_factory

  def test_anthropic_branch_forwards_anthropic_timeout(monkeypatch, tmp_path):
      seen = {}
      monkeypatch.setattr("jsa.server.backend_for",
                          lambda name, **kw: seen.update(name=name, **kw))
      settings = Settings(anthropic_timeout=42.0, db_path=tmp_path / "t.sqlite")
      make_backend_factory(settings)("anthropic")
      assert seen["timeout"] == 42.0   # not agent_timeout
  ```

  Note the branches differ in *which* timeout they forward (`anthropic_timeout` vs
  `agent_timeout`) — that's the behaviour worth pinning, and a plain `assert isinstance`
  check would miss it.
- CORS wiring: `create_app(settings, dev_tunnel=True)` allows `*`; the default allows
  only `http://localhost:\d+`.
- The static-mount branch: `GET /` returns `index.html` with
  `Cache-Control: no-cache, no-store, must-revalidate` when `jsa/static/index.html`
  exists, and 404s when it doesn't.
- Shutdown: the hook sets `orchestrator._stopping = True` and disposes the engine.

For `make_backend_factory` you don't need Recipe B at all — just build a `Settings` and
call it.
