"""JSA CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

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
    out: Path = typer.Option(
        Path("output"),
        "--out",
        help="Output directory for generated PDFs",
    ),
    backend: str = typer.Option(
        "claude-cli",
        "--backend",
        help="AI backend: claude-cli | gemini-cli | anthropic",
    ),
    db: Path = typer.Option(
        Path.home() / ".jsa" / "jsa.sqlite",
        "--db",
        help="SQLite database path",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        help="Port for the local web server",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Do not open browser on start",
        is_flag=True,
    ),
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

    # Validate --backend
    if backend not in _VALID_BACKENDS:
        typer.echo(
            f"Error: --backend must be one of {sorted(_VALID_BACKENDS)}, got: {backend!r}",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo("JSA not yet implemented — scaffold only")


if __name__ == "__main__":
    app()
