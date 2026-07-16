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
    return _REGISTRY[name](**kwargs)


def supports_history_replay(name: str) -> bool:
    """Return whether the backend registered under ``name`` can reconstruct a
    session purely from replayed ``history`` (see
    ``AgentBackend.supports_history_replay``), without instantiating it.

    Used by ``jsa.db.repo.backend_switch_reset`` to decide whether a limit-hit
    stage's Message history can be retained for replay on the new backend, or
    must be discarded (CLI backends, whose sessions live in a native,
    backend-specific store).

    An unregistered ``name`` conservatively returns False — same as "cannot
    replay" — so an unknown target backend gets the safe (full-reset) path
    rather than an incorrect retention path.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        return False
    return getattr(cls, "supports_history_replay", False)


# Register CLI backends
from jsa.agents.claude_cli import ClaudeCliBackend  # noqa: E402
from jsa.agents.google_cli import GoogleCliBackend  # noqa: E402

register("claude-cli", ClaudeCliBackend)
register("google-cli", GoogleCliBackend)

from jsa.agents.anthropic_api import AnthropicAPIBackend  # noqa: E402

register("anthropic", AnthropicAPIBackend)
