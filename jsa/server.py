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

from jsa.agents import model_catalog, model_costs
from jsa.agents.base import AgentBackend
from jsa.config import Settings
from jsa.db.engine import create_engine, create_session_factory, init_db
from jsa.events.bus import bus
from jsa.api.routes_backend_models import router as backend_models_router
from jsa.api.routes_cv_decks import router as cv_decks_router
from jsa.api.routes_cv_structure import router as cv_structure_router
from jsa.api.routes_injection_presets import router as injection_presets_router
from jsa.api.routes_jobs import router as jobs_router
from jsa.api.routes_meta import router as meta_router
from jsa.api.routes_preferences import router as preferences_router
from jsa.api.ws import router as ws_router
from jsa.pipeline.orchestrator import BaseCvResolver, Orchestrator
from jsa.agents.registry import backend_for
from jsa.store import backend_models as backend_models_store
from jsa.store import cv_decks

logger = logging.getLogger(__name__)


def _flat_default_model(settings: Settings, name: str) -> str | None:
    """The backend's flat per-backend default model (`settings.model` /
    `settings.opencode_zen_model` / ...), or None for `google-cli` (no model concept).

    Single source of truth for the backend→flat-default mapping, shared by
    `make_backend_factory`'s `_model_for` and `Orchestrator`'s `model_resolver` (used
    by the Phase 4 model ladder to know a job's *implicit* starting rung when
    `job.model_name` is still unset). Do not re-derive this mapping at a new call
    site — CLAUDE.md documents that doing so once cost the fit gate a 600s timeout
    instead of 180s.
    """
    return {
        "anthropic": settings.model,
        "opencode-zen": settings.opencode_zen_model,
        "mistral": settings.mistral_model,
        "openrouter": settings.openrouter_model,
        "gemini": settings.gemini_model,
        "opencode-go": settings.opencode_go_model,
        "claude-cli": settings.model,
        "google-cli": None,
    }.get(name)


def make_model_resolver(settings: Settings) -> Callable[[str], str | None]:
    """Backend name -> the model a job on it would use if it has never hopped.

    `settings.backend_models[name]` (the runtime UI selection) if set, else that
    backend's flat default. Deliberately does NOT apply `model_override`
    (`--fit-model`) — the ladder's per-job rung is a *general*-pipeline concept; the
    fit gate's pinned model is resolved separately and always wins over any rung, see
    `make_backend_factory`'s `_model_for`.
    """

    def _resolve(name: str) -> str | None:
        return settings.backend_models.get(name) or _flat_default_model(settings, name)

    return _resolve


def make_base_cv_resolver(settings: Settings) -> BaseCvResolver:
    """A job's ``base_cv_id`` -> the deck file the pipeline should read for it.

    Injected into ``Orchestrator`` the same way ``make_model_resolver`` is, and for the
    same reason: ``jsa/pipeline/orchestrator.py`` cannot import ``Settings`` (this module
    imports the orchestrator, so the reverse direction is a circular import).

    All the fallback semantics live in ``cv_decks.resolve_path`` -- requested deck ->
    index default -> first deck with a file on disk -> ``None``. ``None`` is never fatal:
    the orchestrator's gate holds jobs ``pending``, and ``stages._read_base_structure``
    treats it exactly like a missing legacy ``cv_structure.json``.
    """

    async def _resolve(deck_id: str | None) -> Path | None:
        return await cv_decks.resolve_path(settings, deck_id)

    return _resolve


def make_backend_factory(
    settings: Settings,
    *,
    model_override: str | None = None,
    timeout_override: float | None = None,
) -> Callable[[str, str | None], AgentBackend]:
    """Instantiate a backend by name (+ optional per-call model), forwarding the
    appropriate settings.

    Shared by the app's startup (orchestrator + the CV-structure infer endpoint) and
    the CLI's one-shot bootstrap infer call, so both construct backends identically.

    The two overrides exist so a caller can swap the model and/or timeout *without*
    re-deriving the per-backend argument mapping (anthropic takes `anthropic_timeout`,
    CLI backends take `agent_timeout`, opencode-zen/mistral/openrouter/gemini/
    opencode-go each take their own dedicated `<name>_timeout`, and `google-cli`
    takes no model at all). Both default to None, meaning "use the settings value".
    The `fit_assessment` stage is the one caller that passes them — see
    `Settings.fit_model` / `Settings.fit_timeout`.

    Model precedence: an explicit `model_override` (`--fit-model`) ALWAYS wins,
    immovable by anything downstream — this is what makes Phase 4's pinned-fit escape
    sound (a model-ladder hop on a `--fit-model`-pinned fit stage would otherwise be
    silently overridden right back to the pinned model, defeating the whole point of
    hopping). Next, the per-call `model` argument (a job's current model-ladder rung,
    passed by the orchestrator) — this is deliberately ABOVE the runtime UI selection,
    because a job mid-ladder-hop must keep running its hopped-to rung even if the user
    changes the dropdown while it's in flight. Only when neither is present does this
    fall through to `settings.backend_models[name]` (the runtime selection), then the
    backend's flat per-backend default.
    """

    def _model_for(name: str, default: str, per_call: str | None = None) -> str:
        if model_override is not None:
            return model_override
        if per_call is not None:
            return per_call
        return settings.backend_models.get(name) or default

    def _backend_factory(name: str, model: str | None = None) -> AgentBackend:
        if name == "anthropic":
            resolved = _model_for("anthropic", settings.model, model)
            timeout = settings.anthropic_timeout if timeout_override is None else timeout_override
            return backend_for(
                "anthropic", model=resolved, timeout=timeout, prompt_caching=settings.prompt_caching
            )

        if name == "opencode-zen":
            # opencode-zen has its own model catalog (nemotron/gpt/gemini/claude
            # mirrors, not JSA's Claude-only `model` setting), so it never falls
            # back to `settings.model` — only an explicit override, a per-call
            # ladder rung, or a runtime selection for "opencode-zen" applies.
            resolved = _model_for("opencode-zen", settings.opencode_zen_model, model)
            timeout = settings.opencode_zen_timeout if timeout_override is None else timeout_override
            return backend_for("opencode-zen", model=resolved, timeout=timeout)

        if name == "mistral":
            resolved = _model_for("mistral", settings.mistral_model, model)
            timeout = settings.mistral_timeout if timeout_override is None else timeout_override
            return backend_for(
                "mistral", model=resolved, timeout=timeout, prompt_caching=settings.prompt_caching
            )

        if name == "openrouter":
            resolved = _model_for("openrouter", settings.openrouter_model, model)
            timeout = settings.openrouter_timeout if timeout_override is None else timeout_override
            return backend_for(
                "openrouter", model=resolved, timeout=timeout, prompt_caching=settings.prompt_caching
            )

        if name == "gemini":
            resolved = _model_for("gemini", settings.gemini_model, model)
            timeout = settings.gemini_timeout if timeout_override is None else timeout_override
            return backend_for(
                "gemini", model=resolved, timeout=timeout, prompt_caching=settings.prompt_caching
            )

        if name == "opencode-go":
            resolved = _model_for("opencode-go", settings.opencode_go_model, model)
            timeout = settings.opencode_go_timeout if timeout_override is None else timeout_override
            return backend_for(
                "opencode-go", model=resolved, timeout=timeout, prompt_caching=settings.prompt_caching
            )

        timeout = settings.agent_timeout if timeout_override is None else timeout_override
        if name == "claude-cli":
            resolved = _model_for("claude-cli", settings.model, model)
            return backend_for(name, model=resolved, timeout=timeout)
        # google-cli: GoogleCliBackend.__init__ takes no `model` — the agy CLI has no
        # model flag — so a model override, per-call rung, or runtime selection is
        # silently inapplicable to that backend.
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
    # Exposed for GET /api/jobs' "effective model" display (Phase 5) — the same
    # backend->model resolution the orchestrator's ladder uses, so the job-row
    # display and the ladder's implicit starting rung never disagree.
    app.state.model_resolver = make_model_resolver(settings)

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
        # Snapshot at startup for the model ladder's curated catalog (Phase 4 reads this,
        # never a live listing — a failure path must not depend on a network call to the
        # provider that is already failing). A catalog edit made later via the UI needs a
        # restart to reach the ladder; this is a deliberate, documented limitation.
        _catalog_overrides = _saved_models.catalog

        def _model_ladder(backend_name: str) -> list[str]:
            models = model_catalog.merged_catalog(_catalog_overrides).get(backend_name, [])
            return model_costs.cost_ordered(backend_name, models)

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
            # Decks, not the legacy single file: `cv_structure_path=` is left unpassed in
            # production so there is exactly one base-CV source of truth. The resolver
            # handles both the gate's "is any deck usable" question and each job's own
            # `base_cv_id` at dispatch.
            base_cv_resolver=make_base_cv_resolver(settings),
            preferences_path=settings.preferences_path,
            # Provider-account throttling: cap in-flight jobs per backend and jitter the
            # start of each worker, so N simultaneous launches don't hit one API key at once.
            max_parallel_per_backend=settings.max_parallel_per_backend,
            dispatch_stagger_seconds=settings.dispatch_stagger_seconds,
            # Model-first fallback ladder (Phase 4): try the next model on the SAME backend
            # before advancing to the next backend, on availability failures only.
            model_ladder=_model_ladder,
            model_resolver=make_model_resolver(settings),
            fit_model_pinned=settings.fit_model is not None,
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
    app.include_router(cv_decks_router)
    app.include_router(preferences_router)
    app.include_router(backend_models_router)
    app.include_router(injection_presets_router)
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
