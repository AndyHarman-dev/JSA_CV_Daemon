"""Single-file store for the global library of named prompt-injection presets ("doses").

Mirrors ``jsa/store/preferences.py`` / ``jsa/store/backend_models.py``'s pattern exactly: a
missing file returns a valid default (an empty list) rather than ``None`` or a 404, and all
filesystem IO is wrapped in ``asyncio.to_thread`` (CLAUDE.md -> "Concurrency"). The path is
derived from ``Settings.db_path.parent``, so a test pointing ``db_path`` at a tmp dir is
automatically self-isolated.

Presets are **global, not per-job** — they are a reusable clipboard the user pastes into a
job's injection panel. Nothing here touches the ``Job`` row or job state; the per-job
injection itself lives on that row.

Deliberately no cap on these models: the write-side caps live on the route body model in
``jsa/api/routes_injection_presets.py``. Capping here would add a failure mode the two
sibling stores do not have — an over-cap file (hand-edited, or written by an earlier build)
would make ``load()`` raise and the GET 500 forever, with no way to repair it through the API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel

from jsa.config import Settings


class InjectionPreset(BaseModel):
    id: str
    name: str
    prefix: str = ""
    postfix: str = ""
    first_msg: str = ""  # snake_case like every other DTO here — NOT the prototype's `firstMsg`
    saved_at: str = ""  # ISO-8601, client-supplied; display only, never generated server-side


class InjectionPresets(BaseModel):
    presets: list[InjectionPreset] = []


def injection_presets_path(settings: Settings) -> Path:
    return settings.injection_presets_path


def _load_sync(path: Path) -> InjectionPresets:
    if not path.exists():
        return InjectionPresets()
    data = json.loads(path.read_text(encoding="utf-8"))
    return InjectionPresets.model_validate(data)


def _save_sync(path: Path, presets: InjectionPresets) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(presets.model_dump_json(indent=2), encoding="utf-8")


async def read(path: Path) -> InjectionPresets:
    """Path-based loader -- return the presets stored at ``path``, defaulting if absent."""
    return await asyncio.to_thread(_load_sync, path)


async def load(settings: Settings) -> InjectionPresets:
    return await read(injection_presets_path(settings))


async def save(settings: Settings, presets: InjectionPresets) -> None:
    await asyncio.to_thread(_save_sync, injection_presets_path(settings), presets)
