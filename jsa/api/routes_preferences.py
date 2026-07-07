"""HTTP routes for the global app preferences (currently just output/UI language).

  GET /api/preferences   → {"language": "en"}       (always valid — defaults, never 404)
  PUT /api/preferences   → body {"language": "es"}; 422 on a code not in the language catalog
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from jsa.i18n.languages import is_valid
from jsa.store import preferences

router = APIRouter()


class PreferencesBody(BaseModel):
    language: str


def _settings(request: Request):
    return request.app.state.settings


@router.get("/api/preferences")
async def get_preferences(request: Request) -> dict:
    prefs = await preferences.load(_settings(request))
    return {"language": prefs.language}


@router.put("/api/preferences")
async def put_preferences(request: Request, body: PreferencesBody) -> dict:
    if not is_valid(body.language):
        raise HTTPException(status_code=422, detail=f"Unknown language code: {body.language!r}")
    prefs = preferences.Preferences(language=body.language)
    await preferences.save(_settings(request), prefs)
    return {"language": prefs.language}
