"""Prompt file loader: read_prompt(name) -> str, no caching."""

from pathlib import Path
from typing import Literal

_PROMPTS_DIR = Path(__file__).parent

_NAME_TO_FILE: dict[str, str] = {
    "cv_adjust": "PROMPT_CDADJUST.md",
    "cover_letter": "CVL_PROMPT.md",
    "fit_assessment": "PROMPT_FIT_ASSESSMENT.md",
    "infer_structure": "PROMPT_INFER_STRUCTURE.md",
}


def read_prompt(
    name: Literal["cv_adjust", "cover_letter", "fit_assessment", "infer_structure"],
) -> str:
    """Read a prompt file from disk and return its contents as a string.

    No caching — always reads from disk so user edits are picked up immediately.

    Raises KeyError if name is not a registered prompt name.
    """
    filename = _NAME_TO_FILE[name]  # raises KeyError on invalid name
    return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
