"""HTTP routes for the standalone base-CV ``CVDocument`` JSON (the CV Structure Editor).

These endpoints are **job-less**: they read/write the single canonical structure file via
``jsa.store.cv_structure`` and never touch a Job, document row, or job state. The structure is
the source of truth the editor edits and the ``cv_adjust`` stage later consumes.

  GET  /api/cv-structure         → the stored CVDocument (404 if never saved → editor empty state)
  PUT  /api/cv-structure         → validate + persist an edited CVDocument (422 on schema fail)
  POST /api/cv-structure/infer   → infer a CVDocument from an uploaded CV (multipart); streams
                                    5 `infer_progress` events and returns the result (unsaved)
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
from jsa.store import cv_structure

router = APIRouter()


class CvStructureBody(BaseModel):
    structured: dict[str, Any]


def _settings(request: Request):
    return request.app.state.settings


@router.get("/api/cv-structure")
async def get_cv_structure(request: Request) -> dict:
    """Return the stored base CV. 404 if it has never been saved (drives the empty state)."""
    cv = await cv_structure.load(_settings(request))
    if cv is None:
        raise HTTPException(status_code=404, detail="No CV structure saved yet")
    return {"structured": cv.model_dump()}


@router.put("/api/cv-structure")
async def put_cv_structure(request: Request, body: CvStructureBody) -> dict:
    """Validate against the CVDocument schema (422 on the hard gates — contact name, ≥1
    renderable section, not-a-cover-letter) and persist. Returns the canonical stored form."""
    try:
        cv = CVDocument.model_validate(body.structured)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_concise_reason(exc)) from exc
    await cv_structure.save(_settings(request), cv)
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
