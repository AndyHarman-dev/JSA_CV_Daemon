"""Many-decks store: many base ``CVDocument`` files ("decks") plus a denormalized index.

Mirrors ``jsa/store/cv_structure.py``'s single-file pattern, but owns *many* base CVs
instead of one: each deck is a standalone ``CVDocument`` JSON file under
``Settings.cv_decks_dir`` (filename ``<uuid4().hex>.json``), and ``Settings.cv_decks_path``
holds a small index (order, user-given names, a denormalized ``auto_title`` cache, and
``default_id``) so listing decks for the job-row picker never fans out into one file read
per deck. ``CVDocument`` itself gains no id/name field — it stays the pipeline's wire
schema and the editor's export format; identity lives entirely in the index + filename.

Deck ids are always server-generated (``uuid4().hex``) and every path derivation goes
through ``deck_path``, which validates the id against ``_DECK_ID_RE`` before touching the
filesystem -- this is the path-traversal guard: a client-supplied ``../`` id can never
escape ``cv_decks_dir``.

The legacy single-file store (``cv_structure.json``) is migrated by *copying* its bytes
into a new deck the first time the index is loaded and no index file exists yet
(``migrate_legacy``, invoked from ``load_index``) -- the legacy file itself is never
deleted or rewritten, so it remains an inert backup of the pre-decks state forever. See
``.claude/plans/CV_DECKS_PLAN.md`` (Locked decision 4) and
``tests/backend/test_cv_decks_parity.py`` for the parity oracle this preserves.
``migrate_legacy`` serializes its write behind a module-level lock and re-checks the
index file's existence *inside* the lock, immediately before writing -- two concurrent
first-boot callers (e.g. ``GET /api/config`` and the orchestrator's dispatch gate, both
on the same event loop) must mint exactly one deck from the legacy file, not two.

Errors are split into two ``ValueError`` subclasses so the API layer can map them to the
right HTTP status without re-deriving this module's own validation rules: ``InvalidDeckId``
(malformed id / path-traversal attempt, raised by ``deck_path``) -> 400, and
``UnknownDeckId`` (a well-formed id absent from the index) -> 404. Both subclass
``ValueError``, so a caller that only catches ``ValueError`` keeps working unchanged.

All filesystem IO is wrapped in ``asyncio.to_thread`` -- never block the event loop
(CLAUDE.md -> "Concurrency").
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from jsa.config import Settings
from jsa.schema import CVDocument
from jsa.store import cv_structure

logger = logging.getLogger(__name__)

_DECK_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Guards migrate_legacy's write section -- see the module docstring's race-condition note.
_migrate_lock = asyncio.Lock()


class InvalidDeckId(ValueError):
    """A deck id is malformed or attempts path traversal (fails ``_DECK_ID_RE``).

    Raised by ``deck_path``. The API layer maps this to HTTP 400.
    """


class UnknownDeckId(ValueError):
    """A well-formed deck id is not present in the index.

    Raised by the mutating deck operations (``save_deck``, ``rename_deck``,
    ``set_default``, ``duplicate_deck``). The API layer maps this to HTTP 404.
    """


class DeckMeta(BaseModel):
    id: str
    name: str | None = None          # user label; None -> fall back to auto_title
    auto_title: str | None = None    # cached cv.contact.name, refreshed on every save
    has_cv: bool = False             # False for a created-but-never-saved deck slot


class DeckIndex(BaseModel):
    decks: list[DeckMeta] = []
    default_id: str | None = None    # always set while decks is non-empty


# --- paths ------------------------------------------------------------------------------


def index_path(settings: Settings) -> Path:
    return settings.cv_decks_path


def deck_path(settings: Settings, deck_id: str) -> Path:
    """Return the on-disk path for ``deck_id``, validating it first.

    Every deck path derivation must go through this function -- it is the sole
    path-traversal guard between a deck id and ``cv_decks_dir``.
    """
    if not _DECK_ID_RE.match(deck_id):
        raise InvalidDeckId(f"invalid deck id: {deck_id!r}")
    return settings.cv_decks_dir / f"{deck_id}.json"


# --- index IO -----------------------------------------------------------------------------


def _load_index_sync(path: Path) -> DeckIndex:
    if not path.exists():
        return DeckIndex()
    data = json.loads(path.read_text(encoding="utf-8"))
    return DeckIndex.model_validate(data)


def _save_index_sync(path: Path, index: DeckIndex) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(index.model_dump_json(indent=2), encoding="utf-8")


async def read_index(path: Path) -> DeckIndex:
    """Path-based loader -- return the index at ``path``, defaulting if absent.

    Unlike ``load_index``, this never triggers legacy migration -- it is a pure
    read of whatever is (or isn't) at ``path``, mirroring ``preferences.py``'s
    ``read``/``load`` split.
    """
    return await asyncio.to_thread(_load_index_sync, path)


async def load_index(settings: Settings) -> DeckIndex:
    """Return the deck index for ``settings``, migrating a legacy install on first read.

    When the index file does not exist yet, this delegates to ``migrate_legacy`` instead
    of returning a bare default -- a pre-decks install with a single ``cv_structure.json``
    must come back as one deck, not zero.
    """
    path = index_path(settings)
    if await asyncio.to_thread(path.exists):
        return await read_index(path)
    return await migrate_legacy(settings)


async def save_index(settings: Settings, index: DeckIndex) -> None:
    await asyncio.to_thread(_save_index_sync, index_path(settings), index)


# --- deck IO ------------------------------------------------------------------------------


async def load_deck(settings: Settings, deck_id: str) -> CVDocument | None:
    return await cv_structure.read(deck_path(settings, deck_id))


async def save_deck(settings: Settings, deck_id: str, cv: CVDocument) -> None:
    """Write ``cv`` to ``deck_id``'s file, then refresh the index's denormalized cache.

    The deck file stays the source of truth; the index entry (``auto_title``, ``has_cv``)
    is a cache refreshed on every save, per plan decision 5 (the deck-listing endpoint
    must be one cheap index read, never one file read per deck).

    Membership is checked *before* writing the file -- an unknown ``deck_id`` must raise
    without leaving an orphan file on disk.
    """
    index = await load_index(settings)
    meta = next((m for m in index.decks if m.id == deck_id), None)
    if meta is None:
        raise UnknownDeckId(f"unknown deck id: {deck_id!r}")

    await cv_structure.write(deck_path(settings, deck_id), cv)
    meta.auto_title = cv.contact.name
    meta.has_cv = True
    await save_index(settings, index)


async def create_deck(settings: Settings, *, name: str | None = None) -> DeckMeta:
    """Register a new, empty deck slot -- no CV file is written yet (``has_cv=False``).

    Becomes the index's ``default_id`` if it is the first deck. Ids are always minted
    here via ``uuid4().hex``; callers never supply one.
    """
    index = await load_index(settings)
    meta = DeckMeta(id=uuid4().hex, name=name, auto_title=None, has_cv=False)
    index.decks.append(meta)
    if index.default_id is None:
        index.default_id = meta.id
    await save_index(settings, index)
    return meta


async def rename_deck(settings: Settings, deck_id: str, name: str | None) -> DeckMeta:
    index = await load_index(settings)
    for meta in index.decks:
        if meta.id == deck_id:
            meta.name = name
            await save_index(settings, index)
            return meta
    raise UnknownDeckId(f"unknown deck id: {deck_id!r}")


async def set_default(settings: Settings, deck_id: str) -> None:
    index = await load_index(settings)
    if not any(meta.id == deck_id for meta in index.decks):
        raise UnknownDeckId(f"unknown deck id: {deck_id!r}")
    index.default_id = deck_id
    await save_index(settings, index)


async def duplicate_deck(
    settings: Settings, src_id: str, *, name: str | None = None
) -> DeckMeta:
    """Copy ``src_id``'s file content into a brand-new deck id."""
    index = await load_index(settings)
    src_meta = next((m for m in index.decks if m.id == src_id), None)
    if src_meta is None:
        raise UnknownDeckId(f"unknown deck id: {src_id!r}")

    new_id = uuid4().hex
    src_path = deck_path(settings, src_id)
    dst_path = deck_path(settings, new_id)

    has_cv = await asyncio.to_thread(src_path.exists)
    if has_cv:
        await asyncio.to_thread(dst_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, src_path, dst_path)

    new_meta = DeckMeta(id=new_id, name=name, auto_title=src_meta.auto_title, has_cv=has_cv)
    index.decks.append(new_meta)
    await save_index(settings, index)
    return new_meta


async def delete_deck(settings: Settings, deck_id: str) -> None:
    """Remove ``deck_id``'s file (if any) and its index entry.

    Promotes ``decks[0]`` to default if the deleted deck was the default; clears
    ``default_id`` to ``None`` when no decks remain.
    """
    index = await load_index(settings)
    path = deck_path(settings, deck_id)
    await asyncio.to_thread(path.unlink, missing_ok=True)

    index.decks = [m for m in index.decks if m.id != deck_id]
    if index.default_id == deck_id:
        index.default_id = index.decks[0].id if index.decks else None
    await save_index(settings, index)


async def resolve_path(settings: Settings, deck_id: str | None) -> Path | None:
    """The pipeline seam -- return the first *existing* deck file among:
    the requested deck -> the index's ``default_id`` -> index order.

    Pure ``.exists()`` checks: loadability/corruption of the file's content stays
    ``stages._read_base_structure``'s job, exactly as for the legacy single-file store.
    Logs a warning when a requested deck id could not be honored.
    """
    index = await load_index(settings)

    ordered_ids: list[str] = []
    if deck_id is not None:
        ordered_ids.append(deck_id)
    if index.default_id is not None and index.default_id not in ordered_ids:
        ordered_ids.append(index.default_id)
    for meta in index.decks:
        if meta.id not in ordered_ids:
            ordered_ids.append(meta.id)

    for candidate in ordered_ids:
        try:
            path = deck_path(settings, candidate)
        except ValueError:
            continue
        if await asyncio.to_thread(path.exists):
            if deck_id is not None and candidate != deck_id:
                logger.warning(
                    "cv_decks.resolve_path: requested deck %r has no usable CV yet, "
                    "falling back to deck %r",
                    deck_id,
                    candidate,
                )
            return path

    if deck_id is not None:
        logger.warning(
            "cv_decks.resolve_path: no usable deck found for requested id %r", deck_id
        )
    return None


async def migrate_legacy(settings: Settings) -> DeckIndex:
    """Migrate a pre-decks install into a one-deck index, in place, non-destructively.

    If ``cv_structure_path`` exists and parses, mint a deck id, *copy* the file's bytes
    into ``cv_decks/<id>.json``, write an index with that deck as default, and leave the
    legacy file untouched on disk -- never deleted, never rewritten (plan decision 4). A
    corrupt legacy file is treated as absent (a warning is logged); a fresh install with no
    legacy file returns an empty ``DeckIndex()`` *without* writing the index file, so it has
    no on-disk state until the user creates something.

    The actual write is serialized behind ``_migrate_lock``, with the index file's
    existence re-checked *inside* the lock immediately before writing -- two concurrent
    first-boot callers on the same event loop (e.g. ``GET /api/config`` and the
    orchestrator's dispatch gate, both calling ``load_index`` before either has written)
    must mint exactly one deck from the legacy file, not one each. This function stays
    safe to call directly (it is public and tested directly), not just via ``load_index``.
    """
    legacy_path = settings.cv_structure_path
    if not await asyncio.to_thread(legacy_path.exists):
        return DeckIndex()

    try:
        cv = await cv_structure.read(legacy_path)
    except (json.JSONDecodeError, ValidationError):
        # A genuinely corrupt file (bad JSON or a schema violation) is treated as absent,
        # per plan decision 4 -- the gate then reports "no usable CV structure", the same
        # user-visible outcome as today. A non-corruption failure (e.g. a permissions
        # error) is deliberately NOT swallowed here -- it should surface, not be logged as
        # data corruption.
        logger.warning(
            "cv_decks.migrate_legacy: legacy cv_structure.json at %s is corrupt, "
            "treating as absent",
            legacy_path,
        )
        return DeckIndex()

    if cv is None:
        return DeckIndex()

    async with _migrate_lock:
        # Re-check inside the lock: a concurrent caller may have already won the race
        # and written the index while we were waiting for the lock above.
        idx_path = index_path(settings)
        if await asyncio.to_thread(idx_path.exists):
            return await read_index(idx_path)

        deck_id = uuid4().hex
        dst_path = deck_path(settings, deck_id)
        await asyncio.to_thread(dst_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, legacy_path, dst_path)

        meta = DeckMeta(id=deck_id, name=None, auto_title=cv.contact.name, has_cv=True)
        index = DeckIndex(decks=[meta], default_id=deck_id)
        await save_index(settings, index)
        return index
