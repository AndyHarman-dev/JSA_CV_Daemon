"""HTTP routes for the global library of named prompt-injection presets ("doses").

  GET /api/injection-presets   -> {"presets": [...]}   (always valid — defaults, never 404)
  PUT /api/injection-presets   -> body {"presets": [...]}; whole-list replace

The PUT is a **whole-list replace**, not per-item CRUD: add, delete and reorder are all just
a new array from the client, which is the model the design prototype uses. There is no
per-preset endpoint by design.

The caps below (``MAX_PRESETS`` / ``MAX_FIELD_CHARS``) live on the *body* model, not on
``jsa.store.injection_presets``'s own models, so they guard the write path against a runaway
client without ever making an already-stored file unreadable. FastAPI turns a violation into
a 422 automatically, and nothing is persisted when it does.

Presets are global, not per-job — nothing here touches the ``Job`` row or job state.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from jsa.store import injection_presets

router = APIRouter()

MAX_PRESETS = 200
MAX_FIELD_CHARS = 20_000


class InjectionPresetBody(BaseModel):
    id: str = Field(max_length=MAX_FIELD_CHARS)
    name: str = Field(max_length=MAX_FIELD_CHARS)
    prefix: str = Field(default="", max_length=MAX_FIELD_CHARS)
    postfix: str = Field(default="", max_length=MAX_FIELD_CHARS)
    first_msg: str = Field(default="", max_length=MAX_FIELD_CHARS)
    saved_at: str = Field(default="", max_length=MAX_FIELD_CHARS)


class InjectionPresetsBody(BaseModel):
    presets: list[InjectionPresetBody] = Field(default_factory=list, max_length=MAX_PRESETS)


def _settings(request: Request):
    return request.app.state.settings


def _to_dict(presets: injection_presets.InjectionPresets) -> dict:
    return presets.model_dump()


@router.get("/api/injection-presets")
async def get_injection_presets(request: Request) -> dict:
    return _to_dict(await injection_presets.load(_settings(request)))


@router.put("/api/injection-presets")
async def put_injection_presets(request: Request, body: InjectionPresetsBody) -> dict:
    presets = injection_presets.InjectionPresets.model_validate(body.model_dump())
    await injection_presets.save(_settings(request), presets)
    return _to_dict(presets)
