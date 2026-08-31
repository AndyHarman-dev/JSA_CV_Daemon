"""HTTP routes for per-backend runtime model selection.

  GET /api/backend-models            -> {"selected": {...}, "supports_model_selection": {...}}
  GET /api/backend-models/{backend}  -> {"backend", "models", "selected", "source"}
  PUT /api/backend-models            -> body {"backend", "model"}; 422 on an unknown backend
                                         or one that does not support model selection

Deliberately not part of ``/api/config`` -- that endpoint is on the boot path and is raced
against an 8s timeout in the frontend's ``store.hydrateLanguage``; a provider model-listing
fetch (Phase 5) must never hang off it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from jsa.agents.model_catalog import SUPPORTS_MODEL_SELECTION, merged_catalog
from jsa.agents.registry import _REGISTRY
from jsa.store import backend_models

router = APIRouter()


class BackendModelBody(BaseModel):
    backend: str
    model: str


def _settings(request: Request):
    return request.app.state.settings


@router.get("/api/backend-models")
async def get_backend_models(request: Request) -> dict:
    models = await backend_models.load(_settings(request))
    return {
        "selected": models.selected,
        "supports_model_selection": dict(SUPPORTS_MODEL_SELECTION),
    }


@router.get("/api/backend-models/{backend}")
async def get_backend_models_for(request: Request, backend: str) -> dict:
    if backend not in _REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown backend: {backend!r}")

    models = await backend_models.load(_settings(request))
    catalog = merged_catalog(models.catalog)
    return {
        "backend": backend,
        "models": catalog.get(backend, []),
        "selected": models.selected.get(backend),
        "source": "catalog",
    }


@router.put("/api/backend-models")
async def put_backend_model(request: Request, body: BackendModelBody) -> dict:
    if body.backend not in _REGISTRY:
        raise HTTPException(status_code=422, detail=f"Unknown backend: {body.backend!r}")
    if not SUPPORTS_MODEL_SELECTION.get(body.backend, False):
        raise HTTPException(
            status_code=422,
            detail=f"Backend {body.backend!r} does not support model selection",
        )

    settings = _settings(request)
    models = await backend_models.load(settings)
    models.selected[body.backend] = body.model
    await backend_models.save(settings, models)

    settings.backend_models[body.backend] = body.model

    return {"backend": body.backend, "model": body.model}
