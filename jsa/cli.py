"""JSA CLI entry point."""

from __future__ import annotations

import asyncio
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

app = typer.Typer(help="JSA — Job Search Assistant")

_VALID_BACKENDS = {"claude-cli", "gemini-cli", "anthropic"}


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
    backend: Optional[str] = typer.Option(None, "--backend", help="AI backend: claude-cli | gemini-cli | anthropic"),
    db: Optional[Path] = typer.Option(None, "--db", help="SQLite database path"),
    port: Optional[int] = typer.Option(None, "--port", help="Port for the local web server"),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open browser on start", is_flag=True),
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
    if backend is not None:
        if backend not in _VALID_BACKENDS:
            typer.echo(
                f"Error: --backend must be one of {sorted(_VALID_BACKENDS)}, got: {backend!r}",
                err=True,
            )
            raise typer.Exit(code=1)
        overrides["backend"] = backend
    if db is not None:
        overrides["db_path"] = db
    if port is not None:
        overrides["port"] = port
    if no_browser:
        overrides["no_browser"] = True

    settings = Settings(**overrides)

    # Ensure output dir and DB dir exist
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Run async pre-flight: DB bootstrap → CV extract → CSV ingest → recovery sweep
    asyncio.run(_preflight(settings, csv_path=csv, cv_path=cv))

    # Start server
    fastapi_app = create_app(settings)
    if not settings.no_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{settings.port}")
        threading.Thread(target=_open, daemon=True).start()

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
    jobs = load_csv(csv_path)
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
