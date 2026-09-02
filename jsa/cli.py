"""JSA CLI entry point."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from pydantic import ValidationError

from jsa.config import Settings
from jsa.db.engine import create_engine, create_session_factory, init_db
from jsa.db.repo import recovery_sweep, upsert_job
from jsa.ingest.csv_loader import load_csv
from jsa.pipeline.infer_structure import InferError, run_infer
from jsa.server import create_app, make_backend_factory
from jsa.store import cv_structure

# Dump a per-thread Python traceback to stderr on native crashes (e.g. a
# SIGSEGV inside a C extension like WeasyPrint's fontconfig/pango stack)
# instead of a bare "segmentation fault" with no indication of which thread
# or subsystem faulted.
faulthandler.enable()

app = typer.Typer(help="JSA — Job Search Assistant")

_VALID_BACKENDS = {
    "claude-cli", "google-cli", "anthropic", "opencode-zen",
    "mistral", "openrouter", "gemini", "opencode-go",
}  # kept for fast validation before registry import; test-guarded against jsa.agents.registry._REGISTRY drift


def _start_tunnel(port: int) -> None:
    """Spawn a cloudflared quick tunnel and print the public URL once available.

    Runs non-blocking: the subprocess and the stdout-watching thread are both
    daemons that die automatically when the main process exits.
    """
    if not shutil.which("cloudflared"):
        print("[JSA] ERROR: cloudflared not found. Install with: brew install cloudflared")
        raise typer.Exit(1)

    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout
        text=True,
    )

    def _watch() -> None:
        url_pattern = re.compile(r"https://[^\s]+\.trycloudflare\.com")
        if proc.stdout is None:
            return
        found = False
        for line in proc.stdout:
            if not found:
                m = url_pattern.search(line)
                if m:
                    url = m.group(0)
                    print(f"\n[JSA] ✓ Tunnel URL: {url}", flush=True)
                    print(f"   Open this on your phone: {url}\n", flush=True)
                    found = True
            # Keep draining the pipe so cloudflared never stalls
        if not found:
            print("[JSA] WARNING: cloudflared exited without printing a tunnel URL — check your internet connection.", flush=True)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


@app.command()
def main(
    csv: Path = typer.Option(
        ...,
        "--csv",
        help="CSV file with columns: company,role,link,tier,JD",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    cv: Optional[Path] = typer.Option(
        None,
        "--cv",
        help=(
            "CV file (.pdf or .docx) — used once to seed the CV structure "
            "(~/.jsa/cv_structure.json) if none exists yet; ignored afterwards. "
            "The CV Structure Editor is the source of truth from then on."
        ),
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory for generated PDFs"),
    backend: Optional[str] = typer.Option(None, "--backend", help="AI backend (single): claude-cli | google-cli | anthropic | opencode-zen | mistral | openrouter | gemini | opencode-go (backward-compat alias for --backends)"),
    backends: Optional[str] = typer.Option(None, "--backends", help="Comma-separated ordered backend chain, e.g. claude-cli,mistral,openrouter"),
    fit_model: Optional[str] = typer.Option(None, "--fit-model", help="Model for the fit-assessment stage only (e.g. a cheaper/faster one). Defaults to the same model as every other stage. No effect on google-cli, which has no model flag."),
    fit_timeout: Optional[float] = typer.Option(None, "--fit-timeout", help="Per-reply timeout in seconds for the fit-assessment stage only. Defaults to the backend's normal timeout."),
    db: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
    port: Optional[int] = typer.Option(None, "--port", help="Port for the local web server"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open browser on start", is_flag=True),
    dev_tunnel: bool = typer.Option(False, "--dev-tunnel", help="Start a cloudflared quick tunnel for remote/phone access. WARNING: exposes the unauthenticated API publicly — dev use only."),
    dev_auto: bool = typer.Option(False, "--dev-auto", help="Dev-only: auto-answer NEED_INPUT gates via DEV_ANSWERS.json pattern matching.", is_flag=True),
    select_language: bool = typer.Option(False, "--select-language", help="Show a full-screen language picker + boot sequence before the dashboard on first run.", is_flag=True),
    prompt_caching: Optional[bool] = typer.Option(None, "--prompt-caching/--no-prompt-caching", help="Enable/disable provider prompt-caching request fields (mistral, openrouter, gemini, opencode-go). Defaults to on."),
) -> None:
    """Run JSA: process a CSV of job listings with a CV file."""
    # Validate --csv extension
    if csv.suffix.lower() != ".csv":
        typer.echo(f"Error: --csv must be a .csv file, got: {csv}", err=True)
        raise typer.Exit(code=1)

    # Validate --cv extension (only when provided — it's optional now)
    if cv is not None and cv.suffix.lower() not in {".pdf", ".docx"}:
        typer.echo(f"Error: --cv must be a .pdf or .docx file, got: {cv}", err=True)
        raise typer.Exit(code=1)

    # Build Settings — CLI flags override env/defaults
    overrides: dict = {}
    if out is not None:
        overrides["output_dir"] = out

    # --backends takes precedence over --backend; --backend is a single-item alias
    if backends is not None:
        parsed_backends = [b.strip() for b in backends.split(",") if b.strip()]
        invalid = [b for b in parsed_backends if b not in _VALID_BACKENDS]
        if invalid:
            typer.echo(
                f"Error: --backends contains unknown backend(s) {invalid}. "
                f"Must be one of {sorted(_VALID_BACKENDS)}.",
                err=True,
            )
            raise typer.Exit(code=1)
        overrides["backends"] = parsed_backends
        overrides["backend"] = parsed_backends[0]  # keep backend in sync for /api/config
    elif backend is not None:
        if backend not in _VALID_BACKENDS:
            typer.echo(
                f"Error: --backend must be one of {sorted(_VALID_BACKENDS)}, got: {backend!r}",
                err=True,
            )
            raise typer.Exit(code=1)
        overrides["backend"] = backend
        overrides["backends"] = [backend]

    # Pass-through, unvalidated: unlike backends there is no model registry to check
    # against, and the set of valid model IDs changes outside this codebase.
    if fit_model is not None:
        overrides["fit_model"] = fit_model
    if fit_timeout is not None:
        overrides["fit_timeout"] = fit_timeout

    if db is not None:
        overrides["db_path"] = db
    if port is not None:
        overrides["port"] = port
    if no_browser:
        overrides["no_browser"] = True
    if dev_auto:
        overrides["dev_autoanswer"] = True
    if select_language:
        overrides["select_language"] = True
    if prompt_caching is not None:
        overrides["prompt_caching"] = prompt_caching

    try:
        settings = Settings(**overrides)
    except ValidationError as exc:
        # Catches validators pydantic itself must run (e.g. fit_timeout > 0) that have
        # no CLI-level pre-check the way --backend/--backends do above.
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1)
    # Resolve to absolute path now so file-serving works regardless of where
    # the user's shell CWD is when they restart (e.g. after cd frontend && npm run build).
    settings.output_dir = settings.output_dir.resolve()

    # Configure application logging so pipeline errors appear in the console.
    # uvicorn's own log_level="info" only covers uvicorn-internal loggers; JSA
    # loggers (jsa.pipeline.orchestrator, jsa.agents.*) are independent.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # Ensure output dir and DB dir exist
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Run async pre-flight: DB bootstrap → CV-structure bootstrap → CSV ingest → recovery sweep
    asyncio.run(_preflight(settings, csv_path=csv, cv_path=cv))

    # Start server
    fastapi_app = create_app(settings, dev_tunnel=dev_tunnel)
    if not settings.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{settings.port}")
        threading.Thread(target=_open, daemon=True).start()

    if dev_tunnel:
        _start_tunnel(settings.port)

    uvicorn.run(fastapi_app, host="127.0.0.1", port=settings.port, log_level="info")


async def _bootstrap_cv_structure(settings: Settings, cv_path: Optional[Path]) -> None:
    """Seed ``cv_structure.json`` from ``--cv`` exactly once, if it doesn't exist yet.

    The CV Structure Editor is the single source of truth for CV content from then on:
    - structure already exists + ``--cv`` given → note that ``--cv`` is ignored.
    - structure missing + ``--cv`` given        → infer + save it (one LLM call).
    - structure missing + no ``--cv``           → proceed; jobs stay pending until the
      user sets up their CV in the editor (see Orchestrator.run()'s gate).

    Factored out of ``_preflight`` so it can be exercised directly with a
    ``FakeAgentBackend`` in tests.
    """
    structure_exists = await asyncio.to_thread(settings.cv_structure_path.exists)

    if structure_exists:
        if cv_path is not None:
            typer.echo(
                f"Note: CV structure already exists at {settings.cv_structure_path}; "
                "--cv is ignored. Edit your CV in the app's Structure Editor."
            )
        return

    if cv_path is None:
        typer.echo(
            "No CV structure found — jobs will stay pending until you set up your CV "
            "(open the app and use the CV Structure Editor)."
        )
        return

    async def _print_progress(event: dict) -> None:
        if event.get("type") != "infer_progress":
            return
        status = event.get("status", "active")
        label = event.get("label", "")
        if status == "error":
            typer.echo(f"[JSA] CV setup failed: {event.get('message', label)}", err=True)
        elif status == "done":
            typer.echo("[JSA] CV setup complete.")
        else:
            typer.echo(f"[JSA] CV setup {event.get('step')}/{event.get('total')}: {label}")

    backend = make_backend_factory(settings)(settings.backends[0])
    try:
        cv = await run_infer(backend, cv_path, task_id="cli-bootstrap", publish=_print_progress)
    except InferError as exc:
        typer.echo(
            f"Error: could not set up your CV from {cv_path}: {exc}\n"
            "Fix the file and re-run, or start without --cv and build your CV in the "
            "Structure Editor instead.",
            err=True,
        )
        raise typer.Exit(code=1)
    await cv_structure.save(settings, cv)


async def _preflight(settings: Settings, csv_path: Path, cv_path: Optional[Path]) -> None:
    """DB bootstrap → CV-structure bootstrap → CSV ingest → crash-recovery sweep."""
    engine = create_engine(settings.db_path)
    session_factory = create_session_factory(engine)

    # 1. DB bootstrap
    await init_db(engine)

    # 2. Seed the CV structure from --cv, once, if none exists yet.
    await _bootstrap_cv_structure(settings, cv_path)

    # 3. CSV ingest
    jobs, ingest_errors = load_csv(csv_path)
    for err in ingest_errors:
        typer.echo(f"Warning: {err}", err=True)
    async with session_factory() as session:
        for job_data in jobs:
            await upsert_job(session, job_data)
        await session.commit()

    # 4. Crash-recovery sweep
    async with session_factory() as session:
        await recovery_sweep(session)

    await engine.dispose()


if __name__ == "__main__":
    app()
