"""Phase 1 scaffold tests: Settings, CLI validation, prompt stubs, and module imports."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jsa.cli import app
from jsa.config import Settings

runner = CliRunner()

# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_settings_defaults(monkeypatch):
    """Settings() without any JSA_ env vars must produce the documented defaults."""
    # Clear any ambient env vars that could pollute defaults
    for var in ("JSA_PORT", "JSA_BACKEND", "JSA_NO_BROWSER", "JSA_OUTPUT_DIR", "JSA_DB_PATH"):
        monkeypatch.delenv(var, raising=False)

    s = Settings()
    assert s.port == 8765
    assert s.backend == "claude-cli"
    assert s.no_browser is False


def test_settings_env_override(monkeypatch):
    """JSA_PORT env var must override the default port value."""
    monkeypatch.setenv("JSA_PORT", "9000")
    s = Settings()
    assert s.port == 9000


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_missing_args():
    """Invoking the CLI with no arguments must exit with a non-zero code."""
    result = runner.invoke(app, [])
    assert result.exit_code != 0


def test_cli_invalid_csv_extension(tmp_path):
    """--csv with a non-.csv extension must exit with a non-zero code."""
    csv_file = tmp_path / "some_file.txt"
    csv_file.write_text("company,role,link,tier,JD\n")
    cv_file = tmp_path / "some_file.pdf"
    cv_file.write_bytes(b"%PDF-1.4")

    result = runner.invoke(app, ["--csv", str(csv_file), "--cv", str(cv_file)])
    assert result.exit_code != 0


def test_cli_invalid_cv_extension(tmp_path):
    """--cv with a non-.pdf/.docx extension must exit with a non-zero code."""
    csv_file = tmp_path / "some.csv"
    csv_file.write_text("company,role,link,tier,JD\n")
    cv_file = tmp_path / "some.txt"
    cv_file.write_text("plain text resume")

    result = runner.invoke(app, ["--csv", str(csv_file), "--cv", str(cv_file)])
    assert result.exit_code != 0


def test_cli_invalid_backend(tmp_path):
    """--backend with an unrecognised value must exit with a non-zero code."""
    csv_file = tmp_path / "jobs.csv"
    csv_file.write_text("company,role,link,tier,JD\n")
    cv_file = tmp_path / "resume.pdf"
    cv_file.write_bytes(b"%PDF-1.4")

    result = runner.invoke(
        app,
        ["--csv", str(csv_file), "--cv", str(cv_file), "--backend", "badbackend"],
    )
    assert result.exit_code != 0


def test_cli_valid_invocation(tmp_path):
    """Valid --csv, --cv (.pdf), --backend claude-cli must exit 0 and print scaffold message."""
    csv_file = tmp_path / "jobs.csv"
    csv_file.write_text("company,role,link,tier,JD\n")
    cv_file = tmp_path / "resume.pdf"
    cv_file.write_bytes(b"%PDF-1.4")

    result = runner.invoke(
        app,
        ["--csv", str(csv_file), "--cv", str(cv_file), "--backend", "claude-cli"],
    )
    assert result.exit_code == 0
    assert "not yet implemented" in result.output


def test_cli_valid_cv_docx(tmp_path):
    """Valid --csv and --cv (.docx) must also exit 0 and print scaffold message."""
    csv_file = tmp_path / "jobs.csv"
    csv_file.write_text("company,role,link,tier,JD\n")
    cv_file = tmp_path / "resume.docx"
    cv_file.write_bytes(b"PK\x03\x04")  # minimal DOCX (ZIP) magic bytes

    result = runner.invoke(
        app,
        ["--csv", str(csv_file), "--cv", str(cv_file), "--backend", "claude-cli"],
    )
    assert result.exit_code == 0
    assert "not yet implemented" in result.output


# ---------------------------------------------------------------------------
# Prompt stub tests
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "jsa" / "prompts"


def test_prompt_cv_adjust_has_sentinels():
    """PROMPT_CDADJUST.md must contain all three sentinel markers."""
    content = (_PROMPTS_DIR / "PROMPT_CDADJUST.md").read_text()
    assert "<<<NEED_INPUT>>>" in content
    assert "<<<FINAL>>>" in content
    assert "<<<END>>>" in content


def test_prompt_cvl_has_sentinels():
    """CVL_PROMPT.md must contain all three sentinel markers."""
    content = (_PROMPTS_DIR / "CVL_PROMPT.md").read_text()
    assert "<<<NEED_INPUT>>>" in content
    assert "<<<FINAL>>>" in content
    assert "<<<END>>>" in content


# ---------------------------------------------------------------------------
# Package structure tests
# ---------------------------------------------------------------------------

_STUB_MODULES = [
    "jsa.config",
    "jsa.cli",
    "jsa.server",
    "jsa.db.engine",
    "jsa.db.models",
    "jsa.db.repo",
    "jsa.ingest.csv_loader",
    "jsa.ingest.cv_loader",
    "jsa.agents.base",
    "jsa.agents.protocol",
    "jsa.agents.registry",
    "jsa.prompts.loader",
    "jsa.pipeline.orchestrator",
    "jsa.pipeline.stages",
    "jsa.pipeline.state_machine",
    "jsa.render.base",
    "jsa.render.weasy",
    "jsa.render.registry",
    "jsa.events.bus",
    "jsa.events.schema",
    "jsa.api.routes_jobs",
    "jsa.api.routes_meta",
    "jsa.api.ws",
]


@pytest.mark.parametrize("module_path", _STUB_MODULES)
def test_importable_stubs(module_path):
    """Every stub module must be importable without raising any exception."""
    importlib.import_module(module_path)
