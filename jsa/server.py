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
from jsa.api.routes_backend_models import router as backend_models_router
from jsa.api.routes_cv_structure import router as cv_structure_router
from jsa.api.routes_jobs import router as jobs_router
from jsa.api.routes_meta import router as meta_router
from jsa.api.routes_preferences import router as preferences_router
from jsa.api.ws import router as ws_router
from jsa.pipeline.orchestrator import Orchestrator
from jsa.agents.registry import backend_for
from jsa.store import backend_models as backend_models_store

logger = logging.getLogger(__name__)


def make_backend_factory(
    settings: Settings,
    *,
    model_override: str | None = None,
    timeout_override: float | None = None,
) -> Callable[[str], AgentBackend]:
    """Instantiate a backend by name, forwarding the appropriate settings.

    Shared by the app's startup (orchestrator + the CV-structure infer endpoint) and
    the CLI's one-shot bootstrap infer call, so both construct backends identically.

    The two overrides exist so a caller can swap the model and/or timeout *without*
    re-deriving the per-backend argument mapping (anthropic takes `anthropic_timeout`,
    CLI backends take `agent_timeout`, opencode-zen/mistral/openrouter/gemini/
    opencode-go each take their own dedicated `<name>_timeout`, and `google-cli`
    takes no model at all). Both default to None, meaning "use the settings value".
    The `fit_assessment` stage is the one caller that passes them — see
    `Settings.fit_model` / `Settings.fit_timeout`.

    Model precedence (when `model_override` is None): `settings.backend_models[name]`
    (the runtime selection made via PUT /api/backend-models, re-read on every call so a
    live change reaches the next dispatch) if set, else that backend's flat per-backend
    default (`settings.model` / `settings.opencode_zen_model`). An explicit
    `model_override` always wins over both — this is what keeps the fit gate's
    `--fit-model` pinned regardless of runtime model-selection changes.
    """

    def _model_for(name: str, default: str) -> str:
        if model_override is not None:
            return model_override
        return settings.backend_models.get(name) or default

    def _backend_factory(name: str) -> AgentBackend:
        if name == "anthropic":
            model = _model_for("anthropic", settings.model)
            timeout = settings.anthropic_timeout if timeout_override is None else timeout_override
            return backend_for("anthropic", model=model, timeout=timeout)

        if name == "opencode-zen":
            # opencode-zen has its own model catalog (nemotron/gpt/gemini/claude
            # mirrors, not JSA's Claude-only `model` setting), so it never falls
            # back to `settings.model` — only an explicit override or a runtime
            # selection for "opencode-zen" applies.
            model = _model_for("opencode-zen", settings.opencode_zen_model)
            timeout = settings.opencode_zen_timeout if timeout_override is None else timeout_override
            return backend_for("opencode-zen", model=model, timeout=timeout)

        if name == "mistral":
            model = _model_for("mistral", settings.mistral_model)
            timeout = settings.mistral_timeout if timeout_override is None else timeout_override
            return backend_for("mistral", model=model, timeout=timeout)

        if name == "openrouter":
            model = _model_for("openrouter", settings.openrouter_model)
            timeout = settings.openrouter_timeout if timeout_override is None else timeout_override
            return backend_for("openrouter", model=model, timeout=timeout)

        if name == "gemini":
            model = _model_for("gemini", settings.gemini_model)
            timeout = settings.gemini_timeout if timeout_override is None else timeout_override
            return backend_for("gemini", model=model, timeout=timeout)

        if name == "opencode-go":
            model = _model_for("opencode-go", settings.opencode_go_model)
            timeout = settings.opencode_go_timeout if timeout_override is None else timeout_override
            return backend_for("opencode-go", model=model, timeout=timeout)

        timeout = settings.agent_timeout if timeout_override is None else timeout_override
        if name == "claude-cli":
            model = _model_for("claude-cli", settings.model)
            return backend_for(name, model=model, timeout=timeout)
        # google-cli: GoogleCliBackend.__init__ takes no `model` — the agy CLI has no
        # model flag — so a model override or runtime selection is silently
        # inapplicable to that backend.
        return backend_for(name, timeout=timeout)

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

        # Seed the runtime model-selection dict from disk before building the factory, so
        # a selection made in a previous run is honored from the very first dispatch.
        _saved_models = await backend_models_store.load(settings)
        settings.backend_models.update(_saved_models.selected)

        # Exposed for the job-less CV-structure infer endpoint (routes_cv_structure), which
        # needs a backend the same way the orchestrator does. Tests override this post-startup.
        _backend_factory = make_backend_factory(settings)
        app.state.backend_factory = _backend_factory

        if settings.fit_model is not None:
            # Applies per active backend at dispatch time (make_backend_factory), so log
            # once here rather than silently no-op'ing. google-cli is the only backend
            # where fit_model has no effect (GoogleCliBackend takes no model kwarg) — for
            # a mixed chain, report both facts rather than picking one (a chain with any
            # non-google-cli member still gets the override on that member).
            applicable = [b for b in settings.backends if b != "google-cli"]
            if applicable:
                logger.info(
                    "fit gate will use model %s on %s", settings.fit_model, ", ".join(applicable)
                )
            if "google-cli" in settings.backends:
                logger.info(
                    "fit_model=%s does not apply to google-cli (no model flag)",
                    settings.fit_model,
                )

        orchestrator = Orchestrator(
            session_factory,
            _backend_factory,
            settings.backends,
            # Separate model/timeout for the one-shot fit gate only. Inert (identical to
            # _backend_factory) unless JSA_FIT_MODEL / JSA_FIT_TIMEOUT is set.
            fit_backend_factory=make_backend_factory(
                settings,
                model_override=settings.fit_model,
                timeout_override=settings.fit_timeout,
            ),
            output_dir=settings.output_dir,
            cv_structure_path=settings.cv_structure_path,
            preferences_path=settings.preferences_path,
            # Provider-account throttling: cap in-flight jobs per backend and jitter the
            # start of each worker, so N simultaneous launches don't hit one API key at once.
            max_parallel_per_backend=settings.max_parallel_per_backend,
            dispatch_stagger_seconds=settings.dispatch_stagger_seconds,
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
    app.include_router(backend_models_router)
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
