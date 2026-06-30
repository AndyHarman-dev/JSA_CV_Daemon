"""BF-17: CV format rules — prompt spec and CSS spacing."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_PATH = REPO_ROOT / "jsa" / "prompts" / "PROMPT_CDADJUST.md"
CSS_PATH = REPO_ROOT / "jsa" / "render" / "styles.css"


def test_prompt_specifies_json_contact():
    # The CV is now emitted as structured JSON; the program (serializer) owns layout.
    # The prompt must instruct the JSON `contact` object rather than a Markdown header.
    text = PROMPT_PATH.read_text()
    assert "JSON object" in text
    assert "`contact`" in text
    assert "verbatim" in text  # email/phone copied verbatim from the base CV


def test_prompt_no_old_title_instruction():
    text = PROMPT_PATH.read_text()
    assert "only where the original had visual separators" not in text


def test_prompt_specifies_sections_array():
    # Section ordering/structure is part of the schema the prompt documents.
    text = PROMPT_PATH.read_text()
    assert "`sections`" in text
    assert "ordered" in text


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
