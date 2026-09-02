"""Static (+ opportunistically live) per-model cost data and cost-ordering
primitives for the future model ladder (multi-backend-model-select plan).

**This module builds ONLY the data + ordering primitives.** A later phase wires
these into the BF-19 fallback path so that, before advancing to the NEXT
BACKEND on a timeout/limit/unavailable failure, the orchestrator first tries
the next MODEL on the SAME backend, ascending by cost. Nothing in this module
is called from the pipeline yet.

Costs are USD per 1,000,000 OUTPUT tokens -- output tokens dominate spend for
this project's workloads (long generated CVs/cover letters against comparatively
short prompts), so output price alone is used as the ordering key. Input price
is not modeled.

Where this genuinely changes ladder behavior, and where it doesn't:

- **`opencode-zen`'s catalog is ALL free-tier models (`STATIC_COSTS["opencode-zen"]`
  is all `0.0`).** Every model ties on cost, so `cost_ordered` (a *stable* sort)
  degenerates to a no-op -- the ladder just walks the catalog in its existing
  order. This is expected, not a bug: there is no price signal to rank on here.

- **`openrouter` ships exactly ONE model in `DEFAULT_CATALOG`
  (`jsa/agents/model_catalog.py`).** `cost_ordered`/`next_model` work correctly
  against a 1-element list, but a ladder needs >= 2 rungs to ever hop -- so
  despite this backend having the *best* price data available (live per-model
  pricing straight from the provider, see below), it cannot hop at all until
  someone grows its catalog. Not a bug in this module; a property of the
  catalog it's fed.

- **`opencode-go` is the backend where cost ordering genuinely matters.** Its
  23 `_PROTOCOL` (`jsa/agents/opencode_go.py`) models span a real ~54x price
  range (mimo-v2.5 at $0.28/1M output to kimi-k3 at $15.00/1M output) and none
  of them are free. This is why its table below is hand-authored from the
  provider's published pricing page rather than left to fall through
  `cost_for`'s `None`/unknown-cost handling -- an unpriced ladder here would
  silently degrade to catalog (effectively alphabetical) order, defeating the
  entire feature on the one backend it was built for.

Tiered/peak pricing note (opencode-go): a few models bill different rates by
time-of-day or context length. This table uses the BASE tier for each --
DeepSeek V4 Pro and DeepSeek V4 Flash use their Off-Peak price, and Qwen3.7
Plus / Qwen3.6 Plus use their <=256K-context tier -- because (a) it keeps
`STATIC_COSTS` a single deterministic number per model rather than a
schedule/context-dependent function, and (b) JSA's single-CV-sized requests
sit well inside every model's base context tier, so the base tier is also the
realistic price for this project's traffic.
"""

from __future__ import annotations

from typing import Iterable

# ---------------------------------------------------------------------------
# Static cost tables: backend -> model ID -> USD per 1,000,000 OUTPUT tokens.
# ---------------------------------------------------------------------------

# opencode-go: hand-authored from https://opencode.ai/docs/en/go/ (scraped and
# verified against the raw HTML during this phase -- do not re-derive from
# `_PROTOCOL`'s ordering, and do not "correct" these numbers without re-checking
# the live page). Every one of the 23 `_PROTOCOL` (jsa/agents/opencode_go.py)
# models has an entry here -- see the guard test in test_model_costs.py, which
# fails loudly if a new `_PROTOCOL` entry is added without a matching price.
_OPENCODE_GO_COSTS: dict[str, float] = {
    "mimo-v2.5": 0.28,
    "qwen3.8-flash": 0.47,
    "glm-5.3-flash": 0.50,
    "hy3": 0.58,
    "deepseek-v4-flash": 0.66,  # Off-Peak tier
    "deepseek-v4-flash-vision-exp": 0.66,
    "mimo-v2.5-pro": 0.87,
    "longcat-2.0": 1.20,
    "minimax-m3": 1.20,
    "minimax-m2.7": 1.20,
    "minimax-m2.5": 1.20,
    "qwen3.7-plus": 1.60,  # <=256K tier
    "deepseek-v4-pro": 1.98,  # Off-Peak tier
    "hy4-preview": 2.501,
    "qwen3.6-plus": 3.00,  # <=256K tier
    "kimi-k2.7-code": 4.00,
    "kimi-k2.6": 4.00,
    "glm-5.3": 4.40,
    "glm-5.2": 4.40,
    "glm-5.1": 4.40,
    "qwen3.8-max": 6.00,
    "qwen3.7-max": 7.50,
    "kimi-k3": 15.00,
}

# opencode-zen: every catalog model (jsa/agents/model_catalog.py::DEFAULT_CATALOG)
# is a "-free" tier model -- all $0.00. See module docstring: this makes
# cost_ordered a stable no-op here, by design.
_OPENCODE_ZEN_COSTS: dict[str, float] = {
    "nemotron-3-ultra-free": 0.0,
    "nemotron-3.5-lightning-free": 0.0,
    "mimo-v2.5-free": 0.0,
    "ling-3.0-flash-fin-free": 0.0,
    "deepseek-v4-flash-free": 0.0,
}

# anthropic / claude-cli: published first-party API output pricing (per
# CLAUDE.md's own `/claude-api` skill reference, cached 2026-06-24).
_ANTHROPIC_COSTS: dict[str, float] = {
    "claude-haiku-4-5": 5.00,
    "claude-sonnet-5": 10.00,
    "claude-opus-5": 25.00,
}

# mistral: published output pricing for the catalog's model.
_MISTRAL_COSTS: dict[str, float] = {
    "mistral-small-2603": 0.30,
}

# gemini: published output pricing (standard, non-batch, <=200K context tier)
# for the catalog's models, ascending flash-lite -> flash -> flash (newer gen).
_GEMINI_COSTS: dict[str, float] = {
    "gemini-3.1-flash-lite": 0.40,
    "gemini-3-flash-preview": 2.50,
    "gemini-3.6-flash": 2.50,
}

# openrouter: static fallback for its single DEFAULT_CATALOG entry. Live
# pricing (populated by jsa/agents/model_catalog.py::_fetch_openrouter from
# data it already downloads) is preferred over this whenever available -- see
# cost_for() below.
_OPENROUTER_COSTS: dict[str, float] = {
    "nvidia/nemotron-3-nano-30b-a3b": 0.0,  # free-tier per OpenRouter listing
}

STATIC_COSTS: dict[str, dict[str, float]] = {
    "opencode-go": _OPENCODE_GO_COSTS,
    "opencode-zen": _OPENCODE_ZEN_COSTS,
    "anthropic": _ANTHROPIC_COSTS,
    # claude-cli supports model selection and runs the same Claude models as
    # the anthropic backend, so it snapshots the Anthropic table. Kept as a
    # separate dict copy so a future in-place mutation of one backend's table
    # cannot silently mutate the other.
    "claude-cli": dict(_ANTHROPIC_COSTS),
    "mistral": _MISTRAL_COSTS,
    "gemini": _GEMINI_COSTS,
    "openrouter": _OPENROUTER_COSTS,
    # "google-cli" deliberately omitted -- no model concept (see
    # jsa/agents/model_catalog.py's SUPPORTS_MODEL_SELECTION docstring).
}

# ---------------------------------------------------------------------------
# OpenRouter live-pricing cache.
#
# jsa/agents/model_catalog.py::_fetch_openrouter already downloads
# https://openrouter.ai/api/v1/models on every live-listing call (Phase 5); its
# payload carries `pricing.completion` (a USD-per-TOKEN string, e.g.
# "0.0000004"; "0" for :free variants) per model, which that function used to
# discard. It now also pushes that data in here via record_openrouter_pricing()
# so cost_for("openrouter", ...) can prefer real per-model pricing over the
# single static fallback entry above.
#
# Import direction: this module does NOT import jsa.agents.model_catalog (at
# module scope or otherwise) -- model_catalog already imports
# jsa.agents.opencode_go at module scope, so a model_costs -> model_catalog
# edge would risk a cycle if model_catalog ever imported model_costs back (which
# it does, to call record_openrouter_pricing). Keeping the edge one-way
# (model_catalog -> model_costs only) means model_costs has zero import-time
# dependency on model_catalog, which is also the safer direction for a module
# whose whole purpose is passive data: it must stay importable/usable even if
# something about live-fetch wiring changes later.
# ---------------------------------------------------------------------------

_openrouter_live_costs: dict[str, float] = {}


def record_openrouter_pricing(prices: dict[str, float]) -> None:
    """Replace the OpenRouter live-pricing cache with a freshly fetched set.

    Called by `model_catalog._fetch_openrouter` after a successful live
    listing fetch -- never triggers a network call itself. `prices` maps
    model ID -> USD per 1,000,000 output tokens; an empty/partial dict is
    fine (missing models simply keep falling back to `STATIC_COSTS`).
    """
    _openrouter_live_costs.clear()
    _openrouter_live_costs.update(prices)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def cost_for(backend: str, model: str) -> float | None:
    """USD per 1,000,000 output tokens for `model` on `backend`, or `None` if
    unknown. For `backend == "openrouter"`, a live-pricing cache entry (see
    `record_openrouter_pricing` above) is preferred over the static table.
    """
    if backend == "openrouter" and model in _openrouter_live_costs:
        return _openrouter_live_costs[model]
    return STATIC_COSTS.get(backend, {}).get(model)


def cost_ordered(backend: str, models: Iterable[str]) -> list[str]:
    """`models` sorted ascending by `cost_for(backend, ...)`.

    Stable: equal-cost models (including the all-free opencode-zen catalog,
    and any tie between two unknown-cost models) keep their input order.
    Unknown-cost models (cost_for returns None) sort LAST, after every known
    cost, also stably among themselves.
    """
    models_list = list(models)

    def sort_key(model: str) -> tuple[bool, float]:
        cost = cost_for(backend, model)
        return (cost is None, cost if cost is not None else 0.0)

    return sorted(models_list, key=sort_key)


def next_model(backend: str, current: str, models: Iterable[str]) -> str | None:
    """The next rung above `current` in `cost_ordered(backend, models)` order.

    Returns `None` if `current` is the last (most expensive / lowest-priority)
    rung, or if `current` is not present in `models` at all.
    """
    ordered = cost_ordered(backend, models)
    if current not in ordered:
        return None
    idx = ordered.index(current)
    if idx + 1 >= len(ordered):
        return None
    return ordered[idx + 1]
