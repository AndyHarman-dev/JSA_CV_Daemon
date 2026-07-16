"""FastAPI app factory; mounts routes, WS, and static frontend bundle."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse as _FileResponse
from fastapi.staticfiles import StaticFiles

from jsa.agents.base import AgentBackend
from jsa.config import Settings
from jsa.db.engine import create_engine, create_session_factory, init_db
from jsa.events.bus import bus
from jsa.api.routes_cv_structure import router as cv_structure_router
from jsa.api.routes_jobs import router as jobs_router
from jsa.api.routes_meta import router as meta_router
from jsa.api.routes_preferences import router as preferences_router
from jsa.api.ws import router as ws_router
from jsa.pipeline.orchestrator import Orchestrator
from jsa.agents.registry import backend_for

logger = logging.getLogger(__name__)


def make_backend_factory(settings: Settings) -> Callable[[str], AgentBackend]:
    """Instantiate a backend by name, forwarding the appropriate settings.

    Shared by the app's startup (orchestrator + the CV-structure infer endpoint) and
    the CLI's one-shot bootstrap infer call, so both construct backends identically.
    """

    def _backend_factory(name: str) -> AgentBackend:
        if name == "anthropic":
            return backend_for(
                "anthropic",
                model=settings.model,
                timeout=settings.anthropic_timeout,
            )
        if name == "claude-cli":
            return backend_for(name, model=settings.model, timeout=settings.agent_timeout)
        return backend_for(name, timeout=settings.agent_timeout)

    return _backend_factory


async def _warm_up_weasyprint() -> None:
    """Force WeasyPrint/fontconfig's one-time global init to happen alone.

    fontconfig re-parses its config when the font cache looks stale (e.g. after
    a long system sleep). Doing that first real render concurrently with
    another (see jsa/render/weasy.py's _RENDER_LOCK) crashed the process with a
    SIGSEGV inside libfontconfig. The lock already prevents that; this just
    pays the one-time init cost up front so the first real job doesn't stall.
    """
    import os

    if "PYTEST_CURRENT_TEST" in os.environ:  # don't pay a real render on every test's app startup
        return

    from jsa.render.weasy import WeasyPrintRenderer  # noqa: PLC0415

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            await WeasyPrintRenderer().render("warm", Path(tmp.name))
    except Exception:
        logger.warning("WeasyPrint warm-up render failed; continuing anyway", exc_info=True)


def create_app(settings: Settings, dev_tunnel: bool = False) -> FastAPI:
    app = FastAPI(title="JSA", version="1.0")

    # CORS — wide-open for all localhost ports (intentional: local-only tool).
    # When dev_tunnel=True, allow all origins so a cloudflared public URL works.
    if dev_tunnel:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=r"http://localhost:\d+",
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state.settings = settings
    app.state.bus = bus

    @app.on_event("startup")
    async def _startup() -> None:
        engine = create_engine(settings.db_path)
        await init_db(engine)
        session_factory = create_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory

        # Exposed for the job-less CV-structure infer endpoint (routes_cv_structure), which
        # needs a backend the same way the orchestrator does. Tests override this post-startup.
        _backend_factory = make_backend_factory(settings)
        app.state.backend_factory = _backend_factory

        orchestrator = Orchestrator(
            session_factory,
            _backend_factory,
            settings.backends,
            output_dir=settings.output_dir,
            cv_structure_path=settings.cv_structure_path,
            preferences_path=settings.preferences_path,
            enable_fit_assessment=settings.enable_fit_assessment,
        )
        app.state.orchestrator = orchestrator

        await _warm_up_weasyprint()

        asyncio.create_task(orchestrator.run())

        if settings.dev_autoanswer:
            from jsa.dev.autoresponder import DevAutoResponder  # noqa: PLC0415
            responder = DevAutoResponder(session_factory, orchestrator, settings.dev_answers_path)
            app.state.dev_autoresponder = responder
            asyncio.create_task(responder.run())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if hasattr(app.state, "orchestrator"):
            app.state.orchestrator._stopping = True
        if hasattr(app.state, "dev_autoresponder"):
            app.state.dev_autoresponder._stopping = True
        if hasattr(app.state, "engine"):
            await app.state.engine.dispose()

    app.include_router(meta_router)
    app.include_router(jobs_router)
    app.include_router(cv_structure_router)
    app.include_router(preferences_router)
    app.include_router(ws_router)

    # Serve built frontend bundle if present.
    # index.html gets a custom no-cache route so browsers always fetch fresh HTML
    # after a rebuild; hashed JS/CSS assets still benefit from long-lived caching.
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        _index_path = str(static_dir / "index.html")

        @app.get("/")
        async def _serve_index() -> _FileResponse:
            return _FileResponse(
                _index_path,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
