"""REST routes for metadata: /api/health and /api/config."""

from fastapi import APIRouter, Request

from jsa.i18n.languages import LANGUAGES

router = APIRouter()


@router.get("/api/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def config(request: Request):
    settings = request.app.state.settings
    return {
        "backend": settings.backend,
        "backends": settings.backends,
        "output_dir": str(settings.output_dir),
        "db_path": str(settings.db_path),
        "port": settings.port,
        "languages": LANGUAGES,
        "select_language": settings.select_language,
    }
