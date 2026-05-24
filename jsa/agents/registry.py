"""Backend registry: backend_for(name) -> AgentBackend."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jsa.agents.base import AgentBackend

_REGISTRY: dict[str, type["AgentBackend"]] = {}


def register(name: str, cls: type["AgentBackend"]) -> None:
    """Register an AgentBackend subclass under the given name (e.g., 'claude-cli')."""
    _REGISTRY[name] = cls


def backend_for(name: str) -> "AgentBackend":
    """Return an instantiated AgentBackend for the given backend name.

    Raises KeyError with a helpful message if the name is not registered.
    """
    if name not in _REGISTRY:
        available = ", ".join(sorted(_REGISTRY.keys())) or "(none registered)"
        raise KeyError(
            f"Unknown backend {name!r}. Available backends: {available}"
        )
    return _REGISTRY[name]()
