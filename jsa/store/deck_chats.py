"""Per-deck persisted CV-editor chat thread — one small JSON file per deck, mirroring
``jsa/store/preferences.py``'s five-function pattern (``deck_chat_path`` in place of
``preferences_path``, everything else read/load/save/clear).

Reuses ``jsa.store.cv_decks.deck_path``'s id validation (the sole path-traversal guard
between a client-supplied deck id and the filesystem) and its per-loop ``_lock`` helper
(``WeakKeyDictionary``-keyed, NOT a module-level ``asyncio.Lock`` — see that module's
docstring for why a singleton would be poisoned by the first pytest-asyncio loop that
contends it).

Attachment TEXT is deliberately never persisted here — a ``ChatTurn.files`` entry is
just ``{name, size}``. The extracted text is read-only context for the ONE turn it was
attached to (see ``jsa/pipeline/cv_chat.py``'s docstring); keeping it would turn this
file into an unbounded document store.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from jsa.config import Settings
from jsa.store.cv_decks import _lock, deck_path

_MAX_TURNS = 60


class ChatTurn(BaseModel):
    id: str
    role: Literal["user", "agent", "scope"]
    scope: dict[str, Any] = {}
    text: str = ""
    question: str | None = None
    reasoning: str = ""
    items: list[dict[str, Any]] = []
    document: dict[str, Any] | None = None
    status: Literal["pending", "applied", "auto", "discarded", "none", "error"] = "none"
    files: list[dict[str, Any]] = []
    base_hash: str = ""
    created_at: str = ""


class DeckChat(BaseModel):
    turns: list[ChatTurn] = []


def deck_chat_path(settings: Settings, deck_id: str) -> Path:
    """The on-disk path for ``deck_id``'s chat thread, validating the id first via
    ``cv_decks.deck_path`` — that is the sole path-traversal guard between a
    client-supplied deck id and the filesystem; this module must not re-derive it."""
    deck_path(settings, deck_id)  # raises InvalidDeckId on a malformed id
    return settings.deck_chats_dir / f"{deck_id}.json"


def _load_sync(path: Path) -> DeckChat:
    if not path.exists():
        return DeckChat()
    data = json.loads(path.read_text(encoding="utf-8"))
    return DeckChat.model_validate(data)


def _save_sync(path: Path, chat: DeckChat) -> None:
    """Atomic write — temp sibling + ``os.replace`` — mirroring ``cv_decks.py``'s
    ``_save_index_sync`` idiom, so a concurrent reader (``GET .../chat``) can never
    observe a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(chat.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


async def read(path: Path) -> DeckChat:
    """Path-based loader — return the thread at ``path``, defaulting if absent."""
    return await asyncio.to_thread(_load_sync, path)


async def load(settings: Settings, deck_id: str) -> DeckChat:
    return await read(deck_chat_path(settings, deck_id))


async def save(settings: Settings, deck_id: str, chat: DeckChat) -> None:
    """Cap at the last ``_MAX_TURNS`` turns on write, serialized behind a per-deck lock
    so two overlapping saves (unlikely — chat turns are sent one at a time from a
    single editor tab — but cheap to guard) cannot interleave and drop one."""
    async with _lock(f"chat:{deck_id}"):
        capped = DeckChat(turns=chat.turns[-_MAX_TURNS:])
        await asyncio.to_thread(_save_sync, deck_chat_path(settings, deck_id), capped)


async def append(settings: Settings, deck_id: str, *turns: ChatTurn) -> DeckChat:
    """Load, append ``turns``, save, and return the resulting (possibly-capped) thread —
    the single call site every route needs, so no caller re-derives the load-then-save
    sequence itself."""
    async with _lock(f"chat:{deck_id}"):
        path = deck_chat_path(settings, deck_id)
        chat = await read(path)
        chat.turns.extend(turns)
        capped = DeckChat(turns=chat.turns[-_MAX_TURNS:])
        await asyncio.to_thread(_save_sync, path, capped)
        return capped


async def clear(settings: Settings, deck_id: str) -> None:
    async with _lock(f"chat:{deck_id}"):
        await asyncio.to_thread(_save_sync, deck_chat_path(settings, deck_id), DeckChat())


__all__ = ["ChatTurn", "DeckChat", "deck_chat_path", "read", "load", "save", "append", "clear"]
