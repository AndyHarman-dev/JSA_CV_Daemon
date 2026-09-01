"""Application settings loaded from environment variables (prefix: JSA_)."""

from pathlib import Path
from typing import Dict, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JSA_")

    output_dir: Path = Path("output")
    backend: str = "claude-cli"          # claude-cli | google-cli | anthropic | opencode-zen | mistral | openrouter | gemini | opencode-go (kept for backward compat)
    backends: List[str] = ["claude-cli"]  # Ordered chain; backends[0] is the primary
    db_path: Path = Path.home() / ".jsa" / "jsa.sqlite"
    port: int = 8765
    no_browser: bool = False
    model: str = "claude-haiku-4-5"       # Claude model ID for claude-cli and anthropic backends; overridable via JSA_MODEL
    anthropic_timeout: float = 180.0     # Per-reply timeout in seconds, via JSA_ANTHROPIC_TIMEOUT
    agent_timeout: float = 600.0         # Timeout for CLI backends (claude-cli, google-cli), via JSA_AGENT_TIMEOUT
    opencode_zen_model: str = "nemotron-3-ultra-free"  # Model ID for opencode-zen, via JSA_OPENCODE_ZEN_MODEL
    opencode_zen_timeout: float = 180.0  # Per-reply timeout in seconds, via JSA_OPENCODE_ZEN_TIMEOUT
    mistral_model: str = "mistral-small-2603"  # Must match MistralBackend.default_model; via JSA_MISTRAL_MODEL
    mistral_timeout: float = 180.0       # Per-reply timeout in seconds, via JSA_MISTRAL_TIMEOUT
    openrouter_model: str = "nvidia/nemotron-3-nano-30b-a3b"  # Must match OpenRouterBackend.default_model; via JSA_OPENROUTER_MODEL
    openrouter_timeout: float = 180.0    # Per-reply timeout in seconds, via JSA_OPENROUTER_TIMEOUT
    gemini_model: str = "gemini-3.1-flash-lite"  # Must match GeminiBackend.default_model; via JSA_GEMINI_MODEL
    gemini_timeout: float = 180.0        # Per-reply timeout in seconds, via JSA_GEMINI_TIMEOUT
    opencode_go_model: str = "glm-5.3"   # Must match OpenCodeGoBackend.default_model; via JSA_OPENCODE_GO_MODEL
    opencode_go_timeout: float = 180.0   # Per-reply timeout in seconds, via JSA_OPENCODE_GO_TIMEOUT
    fit_model: str | None = None         # Model for the fit_assessment stage only, via JSA_FIT_MODEL / --fit-model.
                                         # None → the fit stage uses the same model as every other stage (`model`).
    fit_timeout: float | None = None     # Per-reply timeout for the fit_assessment stage only, via JSA_FIT_TIMEOUT.
                                         # None → the backend's normal timeout (anthropic_timeout / agent_timeout).
    dev_autoanswer: bool = False          # Dev-only: auto-answer NEED_INPUT gates, via JSA_DEV_AUTOANSWER
    dev_answers_path: Path = Path(__file__).parent / "prompts" / "DEV_ANSWERS.json"
    select_language: bool = False          # --select-language: show the full-screen boot gate (language picker + boot log) before the dashboard on first run
    backend_models: Dict[str, str] = {}   # backend name -> selected model ID, seeded from
                                          # backend_models.json at startup and mutated live by
                                          # PUT /api/backend-models. Overrides the flat
                                          # per-backend default (`model` / `opencode_zen_model`)
                                          # in `make_backend_factory`. Like every field here it
                                          # technically has a JSA_BACKEND_MODELS env alias (JSON
                                          # string, via the shared `env_prefix`), but the intended
                                          # write path is the persisted file / API, not an env var.

    @property
    def cv_structure_path(self) -> Path:
        """Canonical base-CV ``CVDocument`` JSON — the standalone source of truth edited by
        the CV Structure Editor and consumed by the cv_adjust stage. Lives next to the DB
        (``~/.jsa/cv_structure.json`` by default); derived from ``db_path`` so a test that
        points ``db_path`` at a tmp dir is automatically isolated."""
        return self.db_path.parent / "cv_structure.json"

    @property
    def preferences_path(self) -> Path:
        """Global app preferences JSON (currently just ``{"language": "en"}``). Lives next to
        the DB, derived from ``db_path`` the same way ``cv_structure_path`` is."""
        return self.db_path.parent / "preferences.json"

    @property
    def backend_models_path(self) -> Path:
        """Persisted per-backend model selection + user catalog overrides. Lives next to the
        DB, derived from ``db_path`` the same way ``preferences_path`` is."""
        return self.db_path.parent / "backend_models.json"

    @field_validator("backends", mode="before")
    @classmethod
    def _parse_backends(cls, v: object) -> list[str]:
        """Accept a comma-separated string (from env vars) or a list."""
        if isinstance(v, str):
            return [b.strip() for b in v.split(",") if b.strip()]
        return list(v)  # type: ignore[arg-type]

    @field_validator("fit_model", mode="before")
    @classmethod
    def _blank_fit_model_to_none(cls, v: object) -> object:
        """An empty ``--fit-model ""`` should mean "unset", not a literal empty model ID
        forwarded to the backend constructor."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("fit_timeout")
    @classmethod
    def _validate_fit_timeout(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"fit_timeout must be > 0, got {v}")
        return v

    @field_validator("backends")
    @classmethod
    def _validate_backends(cls, v: list[str]) -> list[str]:
        """Validate each backend name against the registry."""
        # Local import inside validator to avoid module-load cycle
        from jsa.agents.registry import _REGISTRY  # noqa: PLC0415
        invalid = [b for b in v if b not in _REGISTRY]
        if invalid:
            available = ", ".join(sorted(_REGISTRY.keys())) or "(none registered)"
            raise ValueError(
                f"Unknown backend(s): {invalid}. Available: {available}"
            )
        return v
