"""Offline shape tests for `jsa/agents/model_catalog.py`. No network."""

from __future__ import annotations

from jsa.agents.model_catalog import DEFAULT_CATALOG, SUPPORTS_MODEL_SELECTION, merged_catalog
from jsa.agents.registry import _REGISTRY
from jsa.config import Settings


def test_every_registered_backend_has_a_catalog_entry():
    for name in _REGISTRY:
        assert name in DEFAULT_CATALOG, f"{name!r} missing from DEFAULT_CATALOG"


def test_every_registered_backend_has_a_selection_support_flag():
    for name in _REGISTRY:
        assert name in SUPPORTS_MODEL_SELECTION, f"{name!r} missing from SUPPORTS_MODEL_SELECTION"


def test_google_cli_does_not_support_model_selection():
    assert SUPPORTS_MODEL_SELECTION["google-cli"] is False
    assert DEFAULT_CATALOG["google-cli"] == []


def test_catalog_entries_are_unique_non_empty_strings():
    for name, models in DEFAULT_CATALOG.items():
        if not SUPPORTS_MODEL_SELECTION.get(name, True):
            continue  # google-cli's empty list is intentional
        assert models, f"{name!r} has an empty catalog but supports selection"
        assert all(isinstance(m, str) and m for m in models)
        assert len(models) == len(set(models)), f"{name!r} has duplicate catalog entries"


def test_default_model_is_a_member_of_its_own_catalog():
    settings = Settings()
    assert settings.model in DEFAULT_CATALOG["claude-cli"]
    assert settings.model in DEFAULT_CATALOG["anthropic"]
    assert settings.opencode_zen_model in DEFAULT_CATALOG["opencode-zen"]


def test_merged_catalog_overrides_replace_not_append():
    overrides = {"anthropic": ["custom-model-a"]}
    merged = merged_catalog(overrides)
    assert merged["anthropic"] == ["custom-model-a"]
    # Untouched backends keep their defaults.
    assert merged["opencode-zen"] == DEFAULT_CATALOG["opencode-zen"]


def test_merged_catalog_with_no_overrides_equals_defaults():
    assert merged_catalog({}) == DEFAULT_CATALOG
