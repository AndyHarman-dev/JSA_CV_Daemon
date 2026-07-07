"""Single-file store for global app preferences (currently just the output/UI language).

Mirrors ``jsa/store/cv_structure.py``'s pattern, with one difference: preferences always have
a valid default (``{"language": "en"}``), so a missing file returns that default rather than
``None`` — there is no empty-state to drive on the frontend, unlike the CV structure. All
filesystem IO is wrapped in ``asyncio.to_thread`` (CLAUDE.md → "Concurrency").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel

from jsa.config import Settings


class Preferences(BaseModel):
    language: str = "en"


def preferences_path(settings: Settings) -> Path:
    return settings.preferences_path


def _load_sync(path: Path) -> Preferences:
    if not path.exists():
        return Preferences()
    data = json.loads(path.read_text(encoding="utf-8"))
    return Preferences.model_validate(data)


def _save_sync(path: Path, prefs: Preferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prefs.model_dump_json(indent=2), encoding="utf-8")


async def read(path: Path) -> Preferences:
    """Path-based loader — return the preferences at ``path``, defaulting if absent.

    Used by the pipeline stages (which hold a path, not a ``Settings``) so the language is
    re-read at stage time and a change applies to the next job/stage without a restart.
    """
    return await asyncio.to_thread(_load_sync, path)


async def load(settings: Settings) -> Preferences:
    return await read(preferences_path(settings))


async def save(settings: Settings, prefs: Preferences) -> None:
    await asyncio.to_thread(_save_sync, preferences_path(settings), prefs)
