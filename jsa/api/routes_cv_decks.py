"""HTTP routes for CV decks (many base CVs, CV Structure Editor's flyout rail).

Mirrors ``jsa/api/routes_cv_structure.py``'s idioms exactly, but exposes the full
``jsa.store.cv_decks`` CRUD surface instead of a single file:

  GET    /api/cv-decks                → every deck (including empty, never-saved slots) +
                                         ``default_id`` — a single cheap index read, never
                                         one file read per deck (plan decision 5)
  POST   /api/cv-decks                → create a new, empty deck slot
  GET    /api/cv-decks/{id}           → the deck's stored CVDocument; 404 if it has no CV yet
  PUT    /api/cv-decks/{id}           → validate + persist a CVDocument into an existing deck
  PATCH  /api/cv-decks/{id}           → rename and/or set as the default deck
  POST   /api/cv-decks/{id}/duplicate → copy a deck's content into a brand-new deck id
  DELETE /api/cv-decks/{id}           → remove a deck (its file and index entry), and
                                         clear the assignment off every UNDISPATCHED job
                                         that referenced it

Error mapping reuses the store's own exception taxonomy rather than re-deriving it here:
``cv_decks.InvalidDeckId`` (malformed / path-traversal id) → 400, ``cv_decks.UnknownDeckId``
(well-formed id absent from the index) → 404. The id-format regex itself lives only in
``cv_decks.deck_path`` — this module never re-runs it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ValidationError

from jsa.db import repo
from jsa.pipeline.infer_structure import _concise_reason
from jsa.schema import CVDocument
from jsa.store import cv_decks
from jsa.store.cv_decks import DeckMeta, InvalidDeckId, UnknownDeckId

router = APIRouter()


class DeckNameBody(BaseModel):
    name: str | None = None


class PatchDeckBody(BaseModel):
    name: str | None = None
    is_default: bool | None = None


class CvStructureBody(BaseModel):
    structured: dict[str, Any]


def _settings(request: Request):
    return request.app.state.settings


def _deck_dict(meta: DeckMeta, default_id: str | None) -> dict:
    return {
        "id": meta.id,
        "name": meta.name,
        "auto_title": meta.auto_title,
        "has_cv": meta.has_cv,
        "is_default": meta.id == default_id,
    }


@router.get("/api/cv-decks")
async def list_cv_decks(request: Request) -> dict:
    """Every deck (empty slots included) — the rail needs to show a just-created slot.

    Filtering to ``has_cv`` decks for the per-job picker is the client's job. This is a
    single index read: it must never fan out into one file read per deck.
    """
    index = await cv_decks.load_index(_settings(request))
    return {
        "decks": [_deck_dict(meta, index.default_id) for meta in index.decks],
        "default_id": index.default_id,
    }


@router.post("/api/cv-decks", status_code=201)
async def create_cv_deck(request: Request, body: DeckNameBody) -> dict:
    settings = _settings(request)
    meta = await cv_decks.create_deck(settings, name=body.name)
    index = await cv_decks.load_index(settings)
    return {"deck": _deck_dict(meta, index.default_id)}


@router.get("/api/cv-decks/{deck_id}")
async def get_cv_deck(request: Request, deck_id: str) -> dict:
    settings = _settings(request)
    try:
        cv = await cv_decks.load_deck(settings, deck_id)
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cv is None:
        raise HTTPException(status_code=404, detail="This deck has no CV saved yet")
    return {"structured": cv.model_dump()}


@router.put("/api/cv-decks/{deck_id}")
async def put_cv_deck(request: Request, deck_id: str, body: CvStructureBody) -> dict:
    """Validate against the CVDocument schema (422 on the hard gates) and persist.

    Returns the canonical stored form and unblocks the orchestrator's gate without a
    restart, exactly like ``PUT /api/cv-structure``.
    """
    settings = _settings(request)
    try:
        cv = CVDocument.model_validate(body.structured)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_concise_reason(exc)) from exc

    try:
        # `save_deck` checks index membership before ever calling `deck_path`, so a
        # malformed id that also happens to be unknown (always true — a malformed id can
        # never be a real index member) would surface as 404 (UnknownDeckId) instead of
        # 400. Validate the format up front so malformed ids consistently 400 here too.
        cv_decks.deck_path(settings, deck_id)
        await cv_decks.save_deck(settings, deck_id, cv)
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownDeckId as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    request.app.state.orchestrator.kick()
    return {"structured": cv.model_dump()}


@router.patch("/api/cv-decks/{deck_id}")
async def patch_cv_deck(request: Request, deck_id: str, body: PatchDeckBody) -> dict:
    """Rename (``name``) and/or set as the default deck (``is_default: true``).

    Setting the default deck ``kick()``s the orchestrator (it changes which deck an
    unassigned job resolves to); a bare rename does not.
    """
    settings = _settings(request)
    try:
        cv_decks.deck_path(settings, deck_id)  # format check only — 400 on malformed id
        if "name" in body.model_fields_set:
            await cv_decks.rename_deck(settings, deck_id, body.name)
        if body.is_default:
            await cv_decks.set_default(settings, deck_id)
            request.app.state.orchestrator.kick()
        index = await cv_decks.load_index(settings)
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownDeckId as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    meta = next((m for m in index.decks if m.id == deck_id), None)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown deck id: {deck_id!r}")
    return {"deck": _deck_dict(meta, index.default_id)}


@router.post("/api/cv-decks/{deck_id}/duplicate", status_code=201)
async def duplicate_cv_deck(request: Request, deck_id: str, body: DeckNameBody) -> dict:
    settings = _settings(request)
    try:
        # Same ordering fix as PUT above — `duplicate_deck` checks membership before
        # `deck_path`, so a malformed id needs its own format check to 400 consistently.
        cv_decks.deck_path(settings, deck_id)
        meta = await cv_decks.duplicate_deck(settings, deck_id, name=body.name)
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnknownDeckId as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    index = await cv_decks.load_index(settings)
    return {"deck": _deck_dict(meta, index.default_id)}


@router.delete("/api/cv-decks/{deck_id}", status_code=204)
async def delete_cv_deck(request: Request, deck_id: str) -> None:
    settings = _settings(request)
    try:
        cv_decks.deck_path(settings, deck_id)  # format check only — 400 on malformed id
    except InvalidDeckId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await cv_decks.delete_deck(settings, deck_id)

    # Clear the now-dangling assignment off jobs that have not been dispatched yet.
    # Scoped to queued+pending inside `repo.clear_base_cv_assignments`: a job already
    # past those states keeps its `base_cv_id` as a record of which base CV it was
    # actually built from (plan: "Deleting a deck clears the assignment on undispatched
    # jobs only"). Routes never write raw SQL — that is why this goes through the repo.
    sf = request.app.state.session_factory
    async with sf() as session:
        await repo.clear_base_cv_assignments(session, deck_id)

    request.app.state.orchestrator.kick()
