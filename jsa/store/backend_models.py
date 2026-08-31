"""Single-file store for per-backend runtime model selection + user catalog overrides.

Mirrors ``jsa/store/preferences.py``'s pattern exactly: a missing file returns a valid
default (empty selection, no catalog overrides) rather than ``None``, and all filesystem
IO is wrapped in ``asyncio.to_thread`` (CLAUDE.md -> "Concurrency").

``selected`` holds the user's chosen model ID per backend name; a backend absent from it
falls back to that backend's flat per-backend default (``Settings.model`` /
``Settings.opencode_zen_model`` / ...) via ``make_backend_factory``'s precedence rule.
``catalog`` holds *user* overrides only -- the code-shipped defaults live in
``jsa/agents/model_catalog.py`` and are merged underneath via ``merged_catalog``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pydantic import BaseModel

from jsa.config import Settings


class BackendModels(BaseModel):
    selected: dict[str, str] = {}
    catalog: dict[str, list[str]] = {}


def backend_models_path(settings: Settings) -> Path:
    return settings.backend_models_path


def _load_sync(path: Path) -> BackendModels:
    if not path.exists():
        return BackendModels()
    data = json.loads(path.read_text(encoding="utf-8"))
    return BackendModels.model_validate(data)


def _save_sync(path: Path, models: BackendModels) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(models.model_dump_json(indent=2), encoding="utf-8")


async def read(path: Path) -> BackendModels:
    """Path-based loader -- return the stored selection at ``path``, defaulting if absent."""
    return await asyncio.to_thread(_load_sync, path)


async def load(settings: Settings) -> BackendModels:
    return await read(backend_models_path(settings))


async def save(settings: Settings, models: BackendModels) -> None:
    await asyncio.to_thread(_save_sync, backend_models_path(settings), models)
