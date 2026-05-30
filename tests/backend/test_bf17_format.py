"""BF-17: CV format rules — prompt spec and CSS spacing."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "jsa" / "prompts" / "PROMPT_CDADJUST.md"
CSS_PATH = REPO_ROOT / "jsa" / "render" / "styles.css"


def test_prompt_header_format():
    text = PROMPT_PATH.read_text()
    assert "No profession title" in text
    assert "# Full Name" in text
    assert "email@example.com | " in text


def test_prompt_no_old_title_instruction():
    text = PROMPT_PATH.read_text()
    assert "only where the original had visual separators" not in text


def test_prompt_unconditional_separator():
    text = PROMPT_PATH.read_text()
    assert "unconditional" in text
    assert "before **every**" in text


def test_css_spacing():
    text = CSS_PATH.read_text()
    # New tightened values present
    assert "margin: 0.75in" in text
    assert "font-size: 10.5pt" in text
    assert "line-height: 1.3" in text
    assert "margin-top: 8pt" in text   # h2
    assert "margin-top: 4pt" in text   # h3
    assert "margin: 6pt 0" in text     # hr

    # Old values gone
    assert "margin: 1in" not in text
    assert "font-size: 11pt" not in text
    assert "line-height: 1.5" not in text
    assert "margin: 12pt 0" not in text
