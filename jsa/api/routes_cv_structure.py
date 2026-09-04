"""HTTP routes for the standalone base-CV ``CVDocument`` JSON (the CV Structure Editor).

The GET/PUT pair here are now thin **default-deck aliases** over ``jsa.store.cv_decks`` —
kept so pre-decks callers (and ``tests/backend/test_cv_source_of_truth.py``) keep working
unchanged (plan decision 3). They are still job-less: they read/write the *default* deck
and never touch a Job, document row, or job state.

  GET  /api/cv-structure         → the default deck's CVDocument (404 if there is no default
                                    deck yet, or it has no CV saved → editor empty state)
  PUT  /api/cv-structure         → validate + persist into the default deck (422 on schema
                                    fail); creates a deck first if the index is empty, so a
                                    fresh install's first PUT still works
  POST /api/cv-structure/infer   → infer a CVDocument from an uploaded CV (multipart); streams
                                    5 `infer_progress` events and returns the result (unsaved).
                                    Deck-agnostic and UNCHANGED — the editor PUTs the result
                                    into whichever deck is active.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, ValidationError

from jsa.events.bus import bus
from jsa.pipeline.infer_structure import InferError, _concise_reason, run_infer
from jsa.schema import CVDocument
from jsa.store import cv_decks

router = APIRouter()


class CvStructureBody(BaseModel):
    structured: dict[str, Any]


def _settings(request: Request):
    return request.app.state.settings


@router.get("/api/cv-structure")
async def get_cv_structure(request: Request) -> dict:
    """Return the default deck's base CV. 404 when there is no default deck yet, or it has
    no CV saved (drives the editor's empty state)."""
    settings = _settings(request)
    index = await cv_decks.load_index(settings)
    cv = await cv_decks.load_deck(settings, index.default_id) if index.default_id else None
    if cv is None:
        raise HTTPException(status_code=404, detail="No CV structure saved yet")
    return {"structured": cv.model_dump()}


@router.put("/api/cv-structure")
async def put_cv_structure(request: Request, body: CvStructureBody) -> dict:
    """Validate against the CVDocument schema (422 on the hard gates — contact name, ≥1
    renderable section, not-a-cover-letter) and persist into the default deck, creating one
    first if the index is empty (keeps a fresh-install PUT working). Returns the canonical
    stored form."""
    settings = _settings(request)
    try:
        cv = CVDocument.model_validate(body.structured)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_concise_reason(exc)) from exc

    # ensure_default_deck, not load_index-then-create: the read and the mint have to share
    # one hold of the index lock, or two concurrent first-install PUTs each create a deck
    # and the loser's save_index clobbers the winner's. See cv_decks.ensure_default_deck.
    default_id = await cv_decks.ensure_default_deck(settings)
    await cv_decks.save_deck(settings, default_id, cv)
    # Unblock the orchestrator's "no CV structure" gate without requiring a restart.
    request.app.state.orchestrator.kick()
    return {"structured": cv.model_dump()}


@router.post("/api/cv-structure/infer")
async def infer_cv_structure(request: Request, file: UploadFile = File(...)) -> dict:
    """Infer a CVDocument from an uploaded CV file. Runs the one-shot inference synchronously,
    broadcasting `infer_progress` events as each step activates, and returns the validated
    (but NOT persisted) structure. The editor saves it via PUT on the user's "Done"."""
    settings = _settings(request)
    backend = request.app.state.backend_factory(settings.backends[0])
    task_id = uuid.uuid4().hex

    suffix = Path(file.filename or "cv").suffix or ".pdf"
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(data)
        tmp.close()
        cv = await run_infer(backend, Path(tmp.name), task_id=task_id, publish=bus.publish)
    except InferError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    return {"task_id": task_id, "structured": cv.model_dump()}
