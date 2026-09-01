"""Live integration test for jsa/agents/model_catalog.py's list_models() fetchers.

Skipped by default (`pytest -m "not integration"`); run explicitly with
`pytest tests/backend/integration/test_model_catalog_live.py -v -m integration`.

This is the "models are valid once specified" check the plan's Phase 7 calls for:
every id this project ships as a `DEFAULT_CATALOG` default is asserted present in
the REAL live `/models` response for that backend, for every backend that has a
live listing API. `tests/backend/test_model_catalog.py` (offline) only checks the
catalog's *shape* — it cannot know whether a hardcoded model id has since been
deprecated or renamed by the provider; only a live call can.

Each backend's key is looked up straight from the environment (this project loads
no .env — see CLAUDE.md), matching every other live-key test in this repo except
that a repo-root `.env` is also consulted as a convenience, mirroring
test_opencode_zen_live.py's helper of the same name.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jsa.agents.model_catalog import DEFAULT_CATALOG, list_models

pytestmark = pytest.mark.integration


def _load_dotenv_key(name: str) -> str | None:
    if name in os.environ:
        return os.environ[name]
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


def _seed_env(*names: str) -> bool:
    """Copy the first present key from .env into os.environ; True if one was found."""
    found = False
    for name in names:
        key = _load_dotenv_key(name)
        if key:
            os.environ[name] = key
            found = True
    return found


# backend -> the env vars that must be present for its live fetcher to run at all
_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "opencode-zen": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"),
    "mistral": ("MISTRAL_API_KEY",),
    "openrouter": (),  # public endpoint, no key required
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
}


@pytest.mark.parametrize("backend", sorted(_REQUIRED_KEYS))
async def test_default_catalog_entries_appear_in_live_listing(backend):
    required = _REQUIRED_KEYS[backend]
    if required and not _seed_env(*required):
        pytest.skip(f"{'/'.join(required)} not set in environment or .env")

    models, source = await list_models(backend)
    if source != "live":
        pytest.fail(
            f"{backend}'s live listing fell back to the catalog — "
            "the fetch itself failed; re-run with -s to see the warning log"
        )

    missing = [m for m in DEFAULT_CATALOG[backend] if m not in models]
    assert not missing, (
        f"{backend}'s DEFAULT_CATALOG contains ids the live /models response no "
        f"longer lists (deprecated or renamed upstream?): {missing}"
    )
