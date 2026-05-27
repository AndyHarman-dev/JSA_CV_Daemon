"""Application settings loaded from environment variables (prefix: JSA_)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JSA_")

    output_dir: Path = Path("output")
    backend: str = "claude-cli"          # claude-cli | gemini-cli | anthropic
    db_path: Path = Path.home() / ".jsa" / "jsa.sqlite"
    port: int = 8765
    no_browser: bool = False
    model: str = "claude-opus-4-7"       # Anthropic model ID, overridable via JSA_MODEL
    anthropic_timeout: float = 180.0     # Per-reply timeout in seconds, via JSA_ANTHROPIC_TIMEOUT
    agent_timeout: float = 300.0         # Timeout for CLI backends (claude-cli, gemini-cli), via JSA_AGENT_TIMEOUT
