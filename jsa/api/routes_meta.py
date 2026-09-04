"""REST routes for metadata: /api/health and /api/config."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from pydantic import ValidationError

from jsa.config import Settings
from jsa.i18n.languages import LANGUAGES
from jsa.store import cv_decks

logger = logging.getLogger(__name__)

router = APIRouter()


def _config_payload(
    settings: Settings, *, cv_structure_exists: bool, cv_deck_count: int
) -> dict:
    """The /api/config body. Factored out so the corrupt-index branch below returns the
    same shape as the happy path -- a missing key here would break the frontend's boot
    just as thoroughly as the 500 that branch exists to avoid."""
    return {
        "backend": settings.backend,
        "backends": settings.backends,
        "output_dir": str(settings.output_dir),
        "db_path": str(settings.db_path),
        "port": settings.port,
        "languages": LANGUAGES,
        "select_language": settings.select_language,
        "cv_structure_exists": cv_structure_exists,
        "cv_deck_count": cv_deck_count,
    }


@router.get("/api/health")
async def health():
    return {"ok": True}


@router.get("/api/config")
async def config(request: Request):
    settings = request.app.state.settings
    try:
        index = await cv_decks.load_index(settings)
    except (json.JSONDecodeError, ValidationError):
        # An unreadable deck index must not 500 this endpoint. /api/config is on the
        # frontend's boot path and is raced against an 8s timeout (CLAUDE.md → "Model
        # selection"), so raising here takes the whole UI down — while the orchestrator's
        # gate and the CLI both already degrade gracefully on this exact input
        # (Orchestrator._base_cv_for_gate, cli._bootstrap_cv_structure). Hardening two of
        # the three readers and leaving this one raising is the inconsistency, not the fix.
        # Same two exceptions only: a permissions/OS error must still surface rather than
        # be reported as "you have no CV" (cv_decks.migrate_legacy's taxonomy).
        #
        # The banner this drives is existence-only by design, so it says "set up your CV",
        # which is not what a corrupt index means. That is deliberate: the orchestrator
        # publishes a LogEvent naming the real cause, and a loaded UI with an imprecise
        # banner beats a UI that never boots. Do not "fix" this by adding a loadability
        # read here — that is exactly what this endpoint may not do.
        logger.warning("GET /api/config: deck index is unreadable — reporting no decks")
        return _config_payload(settings, cv_structure_exists=False, cv_deck_count=0)
    # "cv_structure_exists" now means "at least one deck has a CV" — an .exists() check on
    # the legacy file would be a permanent lie post-migration (the legacy file is left in
    # place forever, see CLAUDE.md → "CV structure — single source of truth"). Note:
    # resolve_path is async, so this must be awaited, not wrapped in bool(...). The
    # index just read is handed to it so this boot-path endpoint pays one index read,
    # not two (and attempts the legacy migration at most once).
    cv_structure_exists = await cv_decks.resolve_path(settings, None, index=index) is not None
    return _config_payload(
        settings,
        cv_structure_exists=cv_structure_exists,
        cv_deck_count=len(index.decks),
    )
