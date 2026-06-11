"""FastAPI app factory; mounts routes, WS, and static frontend bundle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse as _FileResponse
from fastapi.staticfiles import StaticFiles

from jsa.config import Settings
from jsa.db.engine import create_engine, create_session_factory, init_db
from jsa.events.bus import bus
from jsa.api.routes_jobs import router as jobs_router
from jsa.api.routes_meta import router as meta_router
from jsa.api.ws import router as ws_router
from jsa.pipeline.orchestrator import Orchestrator
from jsa.agents.registry import backend_for


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

        def _backend_factory(name: str):
            """Instantiate a backend by name, forwarding the appropriate settings."""
            if name == "anthropic":
                return backend_for(
                    "anthropic",
                    model=settings.model,
                    timeout=settings.anthropic_timeout,
                )
            if name == "claude-cli":
                return backend_for(name, model=settings.model, timeout=settings.agent_timeout)
            return backend_for(name, timeout=settings.agent_timeout)

        orchestrator = Orchestrator(session_factory, _backend_factory, settings.backends, output_dir=settings.output_dir)
        app.state.orchestrator = orchestrator
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
