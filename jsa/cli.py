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

from jsa.config import Settings
from jsa.db.engine import create_engine, create_session_factory, init_db
from jsa.db.repo import recovery_sweep, upsert_job
from jsa.ingest.csv_loader import load_csv
from jsa.ingest.cv_loader import load_cv
from jsa.server import create_app

# Dump a per-thread Python traceback to stderr on native crashes (e.g. a
# SIGSEGV inside a C extension like WeasyPrint's fontconfig/pango stack)
# instead of a bare "segmentation fault" with no indication of which thread
# or subsystem faulted.
faulthandler.enable()

app = typer.Typer(help="JSA — Job Search Assistant")

_VALID_BACKENDS = {"claude-cli", "google-cli", "anthropic"}  # kept for fast validation before registry import


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
    cv: Path = typer.Option(
        ...,
        "--cv",
        help="CV file (.pdf or .docx)",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    out: Optional[Path] = typer.Option(None, "--out", help="Output directory for generated PDFs"),
    backend: Optional[str] = typer.Option(None, "--backend", help="AI backend (single): claude-cli | google-cli | anthropic (backward-compat alias for --backends)"),
    backends: Optional[str] = typer.Option(None, "--backends", help="Comma-separated ordered backend chain, e.g. claude-cli,google-cli"),
    db: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
    port: Optional[int] = typer.Option(None, "--port", help="Port for the local web server"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open browser on start", is_flag=True),
    dev_tunnel: bool = typer.Option(False, "--dev-tunnel", help="Start a cloudflared quick tunnel for remote/phone access. WARNING: exposes the unauthenticated API publicly — dev use only."),
    dev_auto: bool = typer.Option(False, "--dev-auto", help="Dev-only: auto-answer NEED_INPUT gates via DEV_ANSWERS.json pattern matching.", is_flag=True),
) -> None:
    """Run JSA: process a CSV of job listings with a CV file."""
    # Validate --csv extension
    if csv.suffix.lower() != ".csv":
        typer.echo(f"Error: --csv must be a .csv file, got: {csv}", err=True)
        raise typer.Exit(code=1)

    # Validate --cv extension
    if cv.suffix.lower() not in {".pdf", ".docx"}:
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

    if db is not None:
        overrides["db_path"] = db
    if port is not None:
        overrides["port"] = port
    if no_browser:
        overrides["no_browser"] = True
    if dev_auto:
        overrides["dev_autoanswer"] = True

    settings = Settings(**overrides)
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

    # Run async pre-flight: DB bootstrap → CV extract → CSV ingest → recovery sweep
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


async def _preflight(settings: Settings, csv_path: Path, cv_path: Path) -> None:
    """DB bootstrap → CV extract → CSV ingest → crash-recovery sweep."""
    engine = create_engine(settings.db_path)
    session_factory = create_session_factory(engine)

    # 1. DB bootstrap
    await init_db(engine)

    # 2. Extract CV text (blocking — run in thread)
    cv_text = await asyncio.to_thread(load_cv, cv_path)

    # 3. CSV ingest
    jobs, ingest_errors = load_csv(csv_path)
    for err in ingest_errors:
        typer.echo(f"Warning: {err}", err=True)
    async with session_factory() as session:
        for job_data in jobs:
            job_data["cv_text"] = cv_text
            await upsert_job(session, job_data)
        await session.commit()

    # 4. Crash-recovery sweep
    async with session_factory() as session:
        await recovery_sweep(session)

    await engine.dispose()


if __name__ == "__main__":
    app()
