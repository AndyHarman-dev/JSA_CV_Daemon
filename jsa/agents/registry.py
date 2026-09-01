"""Backend registry: backend_for(name) -> AgentBackend."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jsa.agents.base import AgentBackend

_REGISTRY: dict[str, type["AgentBackend"]] = {}


def register(name: str, cls: type["AgentBackend"]) -> None:
    """Register an AgentBackend subclass under the given name (e.g., 'claude-cli')."""
    _REGISTRY[name] = cls


def backend_for(name: str, **kwargs: object) -> "AgentBackend":
    """Return an instantiated AgentBackend for the given backend name.

    Optional kwargs are forwarded to the backend constructor, allowing callers
    to pass model/timeout settings without bypassing the registry.

    Raises KeyError with a helpful message if the name is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none registered)"
        raise KeyError(
            f"Unknown backend {name!r}. Available backends: {available}"
        )
    try:
        return _REGISTRY[name](**kwargs)
    except ValueError as exc:
        # A backend constructor validating its own config (e.g. OpenCodeGoBackend
        # rejecting a model outside its known dual-protocol table) is exactly the
        # "bad model/config" case CLAUDE.md's BF-19 section assigns to
        # AgentBackendUnavailable. Without this, a bad runtime model selection
        # (PUT /api/backend-models does not validate against the catalog) raises a
        # plain ValueError from inside Orchestrator._run_one's try block, which is
        # caught by its generic `except Exception` and hard-fails the job on the
        # very first backend with no BF-19 fallback-chain engagement — the same
        # failure shape CLAUDE.md's OpenCode Zen section documents as already fixed
        # for HTTP-level errors. Re-raising here closes that gap at construction
        # time too, for every backend, not just the ones with HTTP error bodies.
        from jsa.agents.base import AgentBackendUnavailable

        raise AgentBackendUnavailable(
            f"Backend {name!r} rejected its configuration: {exc}"
        ) from exc


# Register CLI backends
from jsa.agents.claude_cli import ClaudeCliBackend  # noqa: E402
from jsa.agents.google_cli import GoogleCliBackend  # noqa: E402

register("claude-cli", ClaudeCliBackend)
register("google-cli", GoogleCliBackend)

from jsa.agents.anthropic_api import AnthropicAPIBackend  # noqa: E402

register("anthropic", AnthropicAPIBackend)

from jsa.agents.opencode_zen import OpenCodeZenBackend  # noqa: E402

register("opencode-zen", OpenCodeZenBackend)

from jsa.agents.mistral import MistralBackend  # noqa: E402
from jsa.agents.openrouter import OpenRouterBackend  # noqa: E402
from jsa.agents.gemini_api import GeminiBackend  # noqa: E402
from jsa.agents.opencode_go import OpenCodeGoBackend  # noqa: E402

register("mistral", MistralBackend)
register("openrouter", OpenRouterBackend)
register("gemini", GeminiBackend)
register("opencode-go", OpenCodeGoBackend)
