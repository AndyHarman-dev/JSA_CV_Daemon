"""Per-backend model catalogs: code-provided defaults + selection support flags +
live model listing with catalog fallback (Phase 5 of the multi-backend-model-select
plan; see `.claude/plans/hi-claude-let-s-implement-floating-blossom.md`).

`DEFAULT_CATALOG` is the fallback list of known-good model IDs for each registered
backend, used whenever no live model-listing API is available or reachable (see
`list_models` below). `backend_models.json` (`jsa/store/backend_models.py`) may carry
*user* catalog overrides on top of this; this module only holds the code-shipped
defaults, so a new default model added here reaches every user without them editing
their JSON.

`SUPPORTS_MODEL_SELECTION` marks which registered backends accept a runtime model choice
at all. `google-cli` shells out to the `agy` CLI, which has no model flag at all
(`GoogleCliBackend.__init__` takes no `model` kwarg) -- it is not merely "no catalog", a
model selection UI for it would be meaningless, so it is called out explicitly rather than
inferred from an empty catalog list.

`opencode-go`'s catalog is derived from `jsa.agents.opencode_go._PROTOCOL` (not
hand-duplicated) -- every entry is guaranteed constructible, closing the gap flagged in
the Phase 2 Change Log where a PUT of a model outside `_PROTOCOL` would 200 and persist,
then hard-fail the next dispatch with an uncaught `ValueError` (none of BF-19's three
typed exceptions). Since the UI only ever offers catalog entries, deriving the catalog
from the same table the constructor validates against means that specific PUT can no
longer happen through the normal selection flow; a hand-crafted `curl PUT` with an
out-of-catalog id remains possible and is a documented residual risk, not a new one.

`opencode-zen`'s catalog deliberately excludes `laguna-s-2.1-free`. It surfaced in the
Phase 0 live `/models` probe as a `-free`-suffixed id, but Phase 2a's docs cross-reference
could not confirm its wire protocol (unlike every other free-tier model, which is
`/chat/completions`-confirmed) -- and `muse-spark-1.2-contributor-free` is live proof that
a `-free` suffix does not guarantee `/chat/completions` on this catalog. Per the plan's
own Change Log ("Phase 5's explicit verification item, not just a residual note"), this
module does not inherit that assumption a third time: the id is withheld from both
`DEFAULT_CATALOG` and the live-listing filter (`_ZEN_FREE_EXCLUDE`) until a user-run probe
confirms its protocol -- see the plan's Verification section for the follow-up command.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable, Literal

import httpx

from jsa.agents.model_costs import record_openrouter_pricing
from jsa.agents.opencode_go import _PROTOCOL as _OPENCODE_GO_PROTOCOL

logger = logging.getLogger(__name__)

DEFAULT_CATALOG: dict[str, list[str]] = {
    "claude-cli": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
    "opencode-zen": [
        "nemotron-3-ultra-free",
        "nemotron-3.5-lightning-free",
        "mimo-v2.5-free",
        "ling-3.0-flash-fin-free",
        "deepseek-v4-flash-free",
        # laguna-s-2.1-free deliberately withheld -- see module docstring.
    ],
    "google-cli": [],
    "mistral": ["mistral-small-2603"],
    "openrouter": ["nvidia/nemotron-3-nano-30b-a3b"],
    "gemini": ["gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-3.6-flash"],
    "opencode-go": sorted(_OPENCODE_GO_PROTOCOL),
}

SUPPORTS_MODEL_SELECTION: dict[str, bool] = {
    "claude-cli": True,
    "anthropic": True,
    "opencode-zen": True,
    "google-cli": False,
    "mistral": True,
    "openrouter": True,
    "gemini": True,
    "opencode-go": True,
}


def merged_catalog(overrides: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge user-provided catalog overrides over the code-provided defaults.

    A backend present in `overrides` replaces its default list entirely (the user is
    curating a specific set for that backend, not appending to the shipped one).
    """
    merged = {name: list(models) for name, models in DEFAULT_CATALOG.items()}
    merged.update({name: list(models) for name, models in overrides.items()})
    return merged


# ---------------------------------------------------------------------------
# Live model listing (Phase 5): fetch where the provider has one, in-process
# TTL cache, silent catalog fallback on any failure. Never raises.
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 300.0
_FETCH_TIMEOUT_SECONDS = 10.0

# backend -> (cached_at (time.monotonic()), model IDs). Only ever written on a
# successful fetch -- a failure must not pin a stale/empty result for the TTL.
_cache: dict[str, tuple[float, list[str]]] = {}

# See module docstring: muse-spark is confirmed non-chat (Phase 2a), laguna is
# unconfirmed -- both are withheld from the live listing, not just the catalog.
_ZEN_FREE_EXCLUDE = {"muse-spark-1.2-contributor-free", "laguna-s-2.1-free"}


async def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict:
    client = httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS)
    try:
        response = await client.get(url, headers=headers or {})
    finally:
        await client.aclose()
    response.raise_for_status()
    return response.json()


def _require_key(*env_vars: str) -> str:
    for env_var in env_vars:
        value = os.environ.get(env_var)
        if value:
            return value
    raise RuntimeError(f"none of {env_vars} is set")


async def _fetch_opencode_zen() -> list[str]:
    api_key = _require_key("OPENCODE_API_KEY")
    body = await _get_json(
        "https://opencode.ai/zen/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    ids = {m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)}
    return sorted(i for i in ids if i.endswith("-free") and i not in _ZEN_FREE_EXCLUDE)


async def _fetch_opencode_go() -> list[str]:
    api_key = _require_key("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY")
    body = await _get_json(
        "https://opencode.ai/zen/go/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    ids = {m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)}
    # Intersected with _PROTOCOL -- a live id this backend can't yet dispatch
    # (unknown protocol) must never reach the UI as selectable.
    return sorted(ids & set(_OPENCODE_GO_PROTOCOL))


async def _fetch_mistral() -> list[str]:
    api_key = _require_key("MISTRAL_API_KEY")
    body = await _get_json(
        "https://api.mistral.ai/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    ids = {m.get("id", "") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")}
    return sorted(ids)


async def _fetch_openrouter() -> list[str]:
    # Public endpoint -- no key required (confirmed Phase 0 probe 1c).
    body = await _get_json("https://openrouter.ai/api/v1/models")
    entries = [m for m in body.get("data", []) if isinstance(m, dict) and m.get("id")]
    ids = {m["id"] for m in entries}
    # Opportunistically push per-model pricing from this same payload into
    # jsa.agents.model_costs -- no new network call, see that module's
    # "OpenRouter live-pricing cache" section. Never breaks listing on
    # malformed/missing pricing data.
    _push_openrouter_pricing(entries)
    return sorted(ids)


def _push_openrouter_pricing(entries: list[dict]) -> None:
    """Extract `pricing.completion` (USD per token, per OpenRouter's /models
    response shape) from an already-fetched payload and record it (converted
    to USD per 1M tokens) via `model_costs.record_openrouter_pricing`.
    Pricing is a side benefit of this fetch, not this function's job -- any
    entry with missing or malformed pricing is skipped, never raised."""
    prices: dict[str, float] = {}
    for entry in entries:
        model_id = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        try:
            per_token = float(pricing.get("completion"))
        except (TypeError, ValueError):
            continue
        prices[model_id] = per_token * 1_000_000
    record_openrouter_pricing(prices)


async def _fetch_gemini() -> list[str]:
    api_key = _require_key("GEMINI_API_KEY", "GOOGLE_API_KEY")
    body = await _get_json(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": api_key},
    )
    ids: set[str] = set()
    for m in body.get("models", []):
        if not isinstance(m, dict):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        name = m.get("name", "")
        # The listing returns "models/gemini-x"; GeminiBackend's request URL is
        # already ".../models/{model}:generateContent" -- an unstripped id here
        # would silently double up to "models/models/..." at dispatch time.
        ids.add(name[len("models/"):] if name.startswith("models/") else name)
    return sorted(i for i in ids if i)


async def _fetch_anthropic() -> list[str]:
    api_key = _require_key("ANTHROPIC_API_KEY")
    body = await _get_json(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    ids = {m.get("id", "") for m in body.get("data", []) if isinstance(m, dict) and m.get("id")}
    return sorted(ids)


# Backends with a live listing API. claude-cli (no listing API) and google-cli (no
# model selection at all) are deliberately absent -- list_models short-circuits
# them straight to the catalog with no network attempt.
_FETCHERS: dict[str, Callable[[], Awaitable[list[str]]]] = {
    "opencode-zen": _fetch_opencode_zen,
    "opencode-go": _fetch_opencode_go,
    "mistral": _fetch_mistral,
    "openrouter": _fetch_openrouter,
    "gemini": _fetch_gemini,
    "anthropic": _fetch_anthropic,
}


async def list_models(
    backend: str, *, catalog_overrides: dict[str, list[str]] | None = None
) -> tuple[list[str], Literal["live", "catalog"]]:
    """Live listing where the provider has one, in-process TTL-cached (5 min);
    falls back to the merged catalog (code defaults + `catalog_overrides`) on any
    fetch failure, timeout, missing key, or empty result -- never raises.

    `catalog_overrides` should be the caller's persisted `BackendModels.catalog`
    (`jsa/store/backend_models.py`) so a live-fetch failure still respects any user
    catalog customization rather than silently reverting to the code defaults.
    """
    fallback = merged_catalog(catalog_overrides or {}).get(backend, [])

    fetcher = _FETCHERS.get(backend)
    if fetcher is None:
        return fallback, "catalog"

    now = time.monotonic()
    cached = _cache.get(backend)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1], "live"

    try:
        models = await fetcher()
        if not models:
            raise RuntimeError(f"{backend} live listing returned no models")
    except Exception as exc:  # noqa: BLE001 -- any failure silently falls back
        logger.warning("live model listing failed for %s, using catalog: %s", backend, exc)
        return fallback, "catalog"

    _cache[backend] = (now, models)
    return models, "live"
