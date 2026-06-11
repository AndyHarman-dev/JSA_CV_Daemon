"""Application settings loaded from environment variables (prefix: JSA_)."""

from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JSA_")

    output_dir: Path = Path("output")
    backend: str = "claude-cli"          # claude-cli | gemini-cli | anthropic (kept for backward compat)
    backends: List[str] = ["claude-cli"]  # Ordered chain; backends[0] is the primary
    db_path: Path = Path.home() / ".jsa" / "jsa.sqlite"
    port: int = 8765
    no_browser: bool = False
    model: str = "claude-haiku-4-5"       # Anthropic model ID, overridable via JSA_MODEL
    anthropic_timeout: float = 180.0     # Per-reply timeout in seconds, via JSA_ANTHROPIC_TIMEOUT
    agent_timeout: float = 300.0         # Timeout for CLI backends (claude-cli, gemini-cli), via JSA_AGENT_TIMEOUT
    dev_autoanswer: bool = False          # Dev-only: auto-answer NEED_INPUT gates, via JSA_DEV_AUTOANSWER
    dev_answers_path: Path = Path(__file__).parent / "prompts" / "DEV_ANSWERS.json"

    @field_validator("backends", mode="before")
    @classmethod
    def _parse_backends(cls, v: object) -> list[str]:
        """Accept a comma-separated string (from env vars) or a list."""
        if isinstance(v, str):
            return [b.strip() for b in v.split(",") if b.strip()]
        return list(v)  # type: ignore[arg-type]

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
