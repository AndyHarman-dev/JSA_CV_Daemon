"""Per-backend model catalogs: code-provided defaults + selection support flags.

`DEFAULT_CATALOG` is the fallback list of known-good model IDs for each registered
backend, used whenever no live model-listing API is available or reachable (see the
Phase 5 `list_models` live-fetch-with-catalog-fallback mechanism). `backend_models.json`
(`jsa/store/backend_models.py`) may carry *user* catalog overrides on top of this; this
module only holds the code-shipped defaults, so a new default model added here reaches
every user without them editing their JSON.

`SUPPORTS_MODEL_SELECTION` marks which registered backends accept a runtime model choice
at all. `google-cli` shells out to the `agy` CLI, which has no model flag at all
(`GoogleCliBackend.__init__` takes no `model` kwarg) -- it is not merely "no catalog", a
model selection UI for it would be meaningless, so it is called out explicitly rather than
inferred from an empty catalog list.
"""

from __future__ import annotations

DEFAULT_CATALOG: dict[str, list[str]] = {
    "claude-cli": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "opencode-zen": [
        "nemotron-3-ultra-free",
        "nemotron-3.5-lightning-free",
        "mimo-v2.5-free",
        "ling-3.0-flash-fin-free",
        "deepseek-v4-flash-free",
        "laguna-s-2.1-free",
    ],
    "google-cli": [],
}

SUPPORTS_MODEL_SELECTION: dict[str, bool] = {
    "claude-cli": True,
    "anthropic": True,
    "opencode-zen": True,
    "google-cli": False,
}


def merged_catalog(overrides: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge user-provided catalog overrides over the code-provided defaults.

    A backend present in `overrides` replaces its default list entirely (the user is
    curating a specific set for that backend, not appending to the shipped one).
    """
    merged = {name: list(models) for name, models in DEFAULT_CATALOG.items()}
    merged.update({name: list(models) for name, models in overrides.items()})
    return merged
