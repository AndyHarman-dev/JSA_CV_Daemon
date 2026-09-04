"""Single-file store for the canonical base-CV ``CVDocument`` JSON.

The CV Structure Editor reshapes one standalone ``CVDocument`` that lives independently of
any job at ``Settings.cv_structure_path`` (``~/.jsa/cv_structure.json`` by default). Jobs are
downstream consumers: the ``cv_adjust`` stage reads this file as the authoritative skeleton.

The file is the source of truth. ``load`` returns ``None`` when it has never been written
(drives the editor's empty/infer state); ``save`` validates before writing, so the on-disk
JSON is always schema-valid. All filesystem IO is wrapped in ``asyncio.to_thread`` — never
block the event loop (CLAUDE.md → "Concurrency").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from jsa.config import Settings
from jsa.schema import CVDocument


def structure_path(settings: Settings) -> Path:
    return settings.cv_structure_path


def _load_sync(path: Path) -> CVDocument | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CVDocument.model_validate(data)


def _save_sync(path: Path, cv: CVDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cv.model_dump_json(indent=2), encoding="utf-8")


async def read(path: Path) -> CVDocument | None:
    """Path-based loader — return the base CV at ``path``, or ``None`` if it does not exist.

    Used by the ``cv_adjust`` stage (which holds a path, not a ``Settings``) so the skeleton
    is re-read at stage time and edits apply to the next job without a restart.
    """
    return await asyncio.to_thread(_load_sync, path)


async def load(settings: Settings) -> CVDocument | None:
    """Return the stored base CV, or ``None`` if the file does not exist yet.

    Raises ``json.JSONDecodeError`` / ``pydantic.ValidationError`` if the file is present but
    corrupt — a hand-edited or stale file is a real error, not silently swallowed.
    """
    return await read(structure_path(settings))


async def write(path: Path, cv: CVDocument) -> None:
    """Path-based writer — persist ``cv`` at ``path``.

    Used by the ``cv_decks`` store (which holds a path per deck, not a single ``Settings``-
    derived location) so both stores share one on-disk write implementation.
    """
    await asyncio.to_thread(_save_sync, path, cv)


async def save(settings: Settings, cv: CVDocument) -> None:
    """Validate-then-write the base CV. Callers pass an already-validated ``CVDocument``."""
    await write(structure_path(settings), cv)
