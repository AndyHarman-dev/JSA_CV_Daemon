"""REST routes for metadata: /api/health and /api/config."""

from fastapi import APIRouter, Request

from jsa.i18n.languages import LANGUAGES
from jsa.store import cv_decks

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def config(request: Request):
    settings = request.app.state.settings
    index = await cv_decks.load_index(settings)
    # "cv_structure_exists" now means "at least one deck has a CV" — an .exists() check on
    # the legacy file would be a permanent lie post-migration (the legacy file is left in
    # place forever, see CLAUDE.md → "CV structure — single source of truth"). Note:
    # resolve_path is async, so this must be awaited, not wrapped in bool(...). The
    # index just read is handed to it so this boot-path endpoint pays one index read,
    # not two (and attempts the legacy migration at most once).
    cv_structure_exists = await cv_decks.resolve_path(settings, None, index=index) is not None
    return {
        "backend": settings.backend,
        "backends": settings.backends,
        "output_dir": str(settings.output_dir),
        "db_path": str(settings.db_path),
        "port": settings.port,
        "languages": LANGUAGES,
        "select_language": settings.select_language,
        "cv_structure_exists": cv_structure_exists,
        "cv_deck_count": len(index.decks),
    }
