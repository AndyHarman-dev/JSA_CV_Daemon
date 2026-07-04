---
status: Done
---

# Fix: Segfault from concurrent WeasyPrint / fontconfig initialization

## Context

The `jsa` server segfaults ("zsh: segmentation fault jsa --csv") after a period of
normal use — in the reported session, shortly after the Mac slept ~20 min and the user
resumed answering `NEED_INPUT` questions and hit `/revise`. This is a **native crash**,
not a Python exception, so it kills the whole server process (all jobs, the WS, the DB).

The macOS crash report `~/Library/Logs/DiagnosticReports/Python-2026-07-04-164541.ips`
(pid 54862, matches the session) names the cause definitively:

```
Exception: EXC_BAD_ACCESS (SIGSEGV), KERN_INVALID_ADDRESS at 0x10
Faulting thread 13 (a to_thread worker):
  libfontconfig  FcPtrListIterInitAtLast          <- fault here
  libfontconfig  FcConfigParseAndLoadFromMemoryInternal / _FcConfigParse
  libexpat       XML_ParseBuffer                  <- parsing fontconfig XML config
  libfontconfig  FcInitLoadOwnConfigAndFonts      <- fontconfig GLOBAL init/re-parse
  libffi / _cffi_backend                          <- WeasyPrint via cffi
  Python thread_run / pythread_wrapper            <- worker thread
```
Four *other* threads (16–19, named `[pango] fontcon…`) are simultaneously parked in
`g_cond_wait` inside the same Pango/fontconfig stack.

**Root cause.** `_render_for_review` (`jsa/pipeline/stages.py:94`) fires the two PDF
renders (cv + cover letter) concurrently via `asyncio.gather`, each on its own
`asyncio.to_thread` worker (`jsa/render/weasy.py:48`). fontconfig's **global
initialization / config re-parse is not thread-safe**. When two render threads enter
`FcInitLoadOwnConfigAndFonts` at the same time, fontconfig's internal pointer list gets
corrupted (deref of `0x10`) → SIGSEGV. With the orchestrator's `asyncio.Semaphore(5)`
(`jsa/pipeline/orchestrator.py:106`), several jobs can enter `review` at once, pushing
even more concurrent renders.

**Why "after sleep / after a while", not on render #1.** fontconfig only re-parses its
config when its font-cache directories look stale (mtime check). Early in the session the
config is already warm, so the concurrent `gather` is harmless. After the 20-min suspend,
the caches look stale, so the next review's renders both trigger a full re-parse
*concurrently* → the race fires. This is intermittent by nature (timing- and cache-state-
dependent), which matches the report.

Ruled out (were competing hypotheses): the `os.killpg(SIGKILL)` cancel path
(`jsa/agents/_subprocess.py:64`) — a bad target raises `ProcessLookupError`, and SIGKILL
would print "killed" not "segmentation fault"; stale `aiosqlite` connections — SQLite
returns error codes, not SIGSEGV; malformed LLM HTML — the crash is in fontconfig *config
init*, never reaching Pango layout of the document body.

**Intended outcome.** WeasyPrint/fontconfig is never entered by more than one thread at a
time, so the init race cannot occur; and any *future* native crash leaves a readable
per-thread Python traceback instead of a bare "segmentation fault".

## Approach

Right-sized fix: **serialize all WeasyPrint renders behind one process-wide lock** (the
matched fix, since the evidence is a concurrency race in init, not bad input), warm
fontconfig once single-threaded at startup, and turn on `faulthandler` as insurance.

### 1. Serialize WeasyPrint renders — `jsa/render/weasy.py` (primary fix)

- Add a **module-level** `asyncio.Lock` (e.g. `_RENDER_LOCK = asyncio.Lock()`). It must be
  module-level, not instance-level: `renderer_for("weasyprint")` returns a **fresh
  `WeasyPrintRenderer()` per call** (`jsa/render/registry.py:16`), so a `self.`-lock would
  not serialize across concurrent jobs.
- In `WeasyPrintRenderer.render`, wrap the existing `await asyncio.to_thread(_write_pdf)`:
  ```python
  async with _RENDER_LOCK:
      await asyncio.to_thread(_write_pdf)
  ```
  One event loop drives every render, so a module-level `asyncio.Lock` guarantees exactly
  one `write_pdf` worker thread runs at any instant, process-wide.
- Leave `DocxRenderer` untouched — it uses lxml/libxml2, not fontconfig/pango, so it can
  still run in parallel with the (now-serialized) PDF render. The `asyncio.gather` in
  `_render_for_review` stays as-is; only the two PDF legs serialize against each other and
  against other jobs' PDFs, so latency impact is minimal.

### 2. Warm fontconfig once at startup, single-threaded — `jsa/server.py` startup

- In the existing startup hook (`jsa/server.py:49`, the `@app.on_event("startup")` block),
  after the engine/orchestrator are set up, do one throwaway render on the main thread
  before any job can run, e.g. `WeasyPrintRenderer` rendering a tiny `"# warm"` string to a
  temp path (or `weasyprint.HTML(string="<p>warm</p>").write_pdf(...)` to a `BytesIO`).
  This forces the racy `FcInitLoadOwnConfigAndFonts` to happen exactly once, alone, so the
  first *real* render never pays init cost under concurrency. Wrap in `try/except` +
  `asyncio.to_thread` so a warm-up failure logs a warning but never blocks startup.
- This is defense-in-depth; the lock alone already prevents the crash, but warming removes
  the first-render latency spike and covers the very first render too.

### 3. Enable faulthandler — `jsa/cli.py` (insurance)

- Near the top of the CLI entrypoint (`jsa/cli.py`), add `import faulthandler;
  faulthandler.enable()` (or set `PYTHONFAULTHANDLER=1` in the launch env). Nearly free; on
  any future SIGSEGV it dumps a per-thread Python traceback to stderr, so a recurrence
  instantly shows which thread/subsystem faulted.

### Rejected alternative

Running WeasyPrint in a **separate process** (subprocess renderer, reusing the
`jsa/agents/_subprocess.py` seam) so a native fault becomes a catchable error. Heavier
(IPC, temp files, serialization of markdown/CSS) and unnecessary: the crash is a
concurrency race in init that a lock fully closes, not an unavoidable fault on bad input.
Can be revisited if faulthandler ever shows a *non-init* WeasyPrint crash.

## Critical files

- `jsa/render/weasy.py` — add module-level `asyncio.Lock`, wrap `to_thread` (the fix).
- `jsa/render/registry.py:16` — confirms fresh-instance-per-call (why lock is module-level).
- `jsa/pipeline/stages.py:72-99` — `_render_for_review`, the concurrent `gather` call site.
- `jsa/server.py:49` — startup hook for the fontconfig warm-up.
- `jsa/cli.py` — entrypoint for `faulthandler.enable()`.

## Verification

1. **Unit test (new):** `tests/backend/test_render_serialization.py` — assert that two
   `WeasyPrintRenderer.render` coroutines launched with `asyncio.gather` never overlap.
   Instrument by monkeypatching the module's `_write_pdf`/`asyncio.to_thread` (or a small
   counter that increments on enter and asserts it never exceeds 1) to prove the lock
   serializes. This is the regression guard (the segfault itself isn't directly testable).
2. **Existing render tests still pass:** `pytest tests/backend -k "render or weasy or stage" -v`.
3. **Full backend suite:** `pytest -v -m "not integration"` — no new failures beyond the
   known pre-existing debt (see memory: 27 backend / 9 frontend fails on HEAD).
4. **Manual smoke:** `jsa --csv <csv> --cv <cv> --no-browser`, drive several jobs to
   `review`/`revise` concurrently; confirm `cv.pdf` + `cover_letter.pdf` render and the
   server stays up. Optionally reproduce the stale-cache path by touching the fontconfig
   cache dir mtime (or `rm -rf ~/.cache/fontconfig`) before a batch of concurrent revises
   and confirm no segfault.
5. **faulthandler confirmation:** verify `faulthandler` is enabled (a future crash would
   now print a Python traceback to the terminal instead of a bare "segmentation fault").

## Branch

Per project policy, create `fix/weasyprint-fontconfig-segfault` from `main` before any
edits. Do not merge without explicit approval.
