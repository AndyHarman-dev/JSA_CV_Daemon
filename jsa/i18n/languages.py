"""Curated language catalog: the single source of truth for the language picker.

Served to the frontend via ``GET /api/config`` (``languages`` key) and used server-side both
to validate ``PUT /api/preferences`` and to resolve a human-readable name for the pipeline's
language directive (see ``jsa/pipeline/stages.py``). The frontend must not hand-copy this
list — it always reads it from ``/api/config``.
"""

from __future__ import annotations

# (ISO 639-1 code, English name, native name)
LANGUAGES: list[tuple[str, str, str]] = [
    ("en", "English", "English"),
    ("es", "Spanish", "Español"),
    ("fr", "French", "Français"),
    ("de", "German", "Deutsch"),
    ("pt", "Portuguese", "Português"),
    ("it", "Italian", "Italiano"),
    ("nl", "Dutch", "Nederlands"),
    ("sv", "Swedish", "Svenska"),
    ("pl", "Polish", "Polski"),
    ("ru", "Russian", "Русский"),
    ("tr", "Turkish", "Türkçe"),
    ("ar", "Arabic", "العربية"),
    ("he", "Hebrew", "עברית"),
    ("hi", "Hindi", "हिन्दी"),
    ("zh", "Chinese", "中文"),
    ("ja", "Japanese", "日本語"),
    ("ko", "Korean", "한국어"),
    ("vi", "Vietnamese", "Tiếng Việt"),
    ("th", "Thai", "ไทย"),
    ("id", "Indonesian", "Bahasa Indonesia"),
]

CODES: frozenset[str] = frozenset(code for code, _, _ in LANGUAGES)

_NAMES: dict[str, str] = {code: name for code, name, _ in LANGUAGES}


def is_valid(code: str) -> bool:
    return code in CODES


def language_name(code: str) -> str:
    """English name for ``code``, falling back to the code itself if unknown."""
    return _NAMES.get(code, code)
