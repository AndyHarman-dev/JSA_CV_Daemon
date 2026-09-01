"""Offline shape tests for `jsa/agents/model_catalog.py`, plus mocked-httpx tests for
`list_models`'s live-fetch-with-catalog-fallback mechanism (Phase 5). The shape tests
below never touch the network; the `TestListModels*` classes patch `httpx.AsyncClient`
the same way `tests/backend/test_openai_compat.py` does."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx as httpx_module
import pytest

import jsa.agents.model_catalog as model_catalog
from jsa.agents.model_catalog import (
    DEFAULT_CATALOG,
    SUPPORTS_MODEL_SELECTION,
    list_models,
    merged_catalog,
)
from jsa.agents.registry import _REGISTRY
from jsa.config import Settings


@pytest.fixture(autouse=True)
def _clear_model_catalog_cache():
    """The live-listing TTL cache is process-global -- isolate every test."""
    model_catalog._cache.clear()
    yield
    model_catalog._cache.clear()


def _mock_client(json_body, status_code: int = 200) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = json_body
    mock_response.raise_for_status = MagicMock()
    if status_code >= 400:
        mock_response.raise_for_status.side_effect = httpx_module.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response
        )
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.aclose = AsyncMock()
    return mock_client


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
    assert settings.mistral_model in DEFAULT_CATALOG["mistral"]
    assert settings.openrouter_model in DEFAULT_CATALOG["openrouter"]
    assert settings.gemini_model in DEFAULT_CATALOG["gemini"]
    assert settings.opencode_go_model in DEFAULT_CATALOG["opencode-go"]


def test_openrouter_catalog_entries_are_namespaced():
    """OpenRouter IDs are vendor/model -- a bare id is a 4xx at dispatch time."""
    for model_id in DEFAULT_CATALOG["openrouter"]:
        assert "/" in model_id, f"{model_id!r} is not namespaced vendor/model"


def test_opencode_go_catalog_is_derived_from_protocol_table():
    """Every catalog entry must be constructible -- see module docstring."""
    from jsa.agents.opencode_go import _PROTOCOL

    assert set(DEFAULT_CATALOG["opencode-go"]) <= set(_PROTOCOL)
    assert DEFAULT_CATALOG["opencode-go"], "opencode-go catalog must not be empty"


def test_merged_catalog_overrides_replace_not_append():
    overrides = {"anthropic": ["custom-model-a"]}
    merged = merged_catalog(overrides)
    assert merged["anthropic"] == ["custom-model-a"]
    # Untouched backends keep their defaults.
    assert merged["opencode-zen"] == DEFAULT_CATALOG["opencode-zen"]


def test_merged_catalog_with_no_overrides_equals_defaults():
    assert merged_catalog({}) == DEFAULT_CATALOG


class TestListModelsNoListingApi:
    async def test_claude_cli_returns_catalog_with_no_network(self):
        with patch("httpx.AsyncClient") as mock_ctor:
            models, source = await list_models("claude-cli")

        mock_ctor.assert_not_called()
        assert models == DEFAULT_CATALOG["claude-cli"]
        assert source == "catalog"

    async def test_google_cli_returns_empty_catalog_with_no_network(self):
        with patch("httpx.AsyncClient") as mock_ctor:
            models, source = await list_models("google-cli")

        mock_ctor.assert_not_called()
        assert models == []
        assert source == "catalog"


class TestListModelsLiveFetch:
    async def test_mistral_live_fetch_returns_sorted_ids(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        body = {"data": [{"id": "mistral-large"}, {"id": "mistral-small-2603"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("mistral")

        assert models == ["mistral-large", "mistral-small-2603"]
        assert source == "live"

    async def test_openrouter_live_fetch_needs_no_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        body = {"data": [{"id": "nvidia/nemotron-3-nano-30b-a3b"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("openrouter")

        assert models == ["nvidia/nemotron-3-nano-30b-a3b"]
        assert source == "live"

    async def test_gemini_live_fetch_strips_models_prefix(self, monkeypatch):
        """The real API returns "models/gemini-x"; GeminiBackend's URL already has
        the "models/" segment, so an unstripped id would 404 only at dispatch."""
        monkeypatch.setenv("GEMINI_API_KEY", "key")
        body = {
            "models": [
                {
                    "name": "models/gemini-3.1-flash-lite",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/embedding-001",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("gemini")

        assert models == ["gemini-3.1-flash-lite"]
        assert source == "live"

    async def test_gemini_uses_x_goog_api_key_header(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "secret")
        mock_client = _mock_client({"models": []})
        with patch("httpx.AsyncClient", return_value=mock_client):
            await list_models("gemini")

        _, kwargs = mock_client.get.call_args
        assert kwargs["headers"]["x-goog-api-key"] == "secret"

    async def test_opencode_zen_live_fetch_filters_to_free_tier(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_API_KEY", "key")
        body = {
            "data": [
                {"id": "nemotron-3-ultra-free"},
                {"id": "claude-sonnet-5"},  # not free-tier -- excluded
                {"id": "muse-spark-1.2-contributor-free"},  # confirmed non-chat -- excluded
                {"id": "laguna-s-2.1-free"},  # protocol unconfirmed -- excluded
            ]
        }
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("opencode-zen")

        assert models == ["nemotron-3-ultra-free"]
        assert source == "live"

    async def test_opencode_go_live_fetch_intersects_with_protocol_table(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        body = {"data": [{"id": "glm-5.3"}, {"id": "some-brand-new-unmapped-model"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("opencode-go")

        assert models == ["glm-5.3"]
        assert source == "live"

    async def test_anthropic_live_fetch(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
        body = {"data": [{"id": "claude-sonnet-5"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("anthropic")

        assert models == ["claude-sonnet-5"]
        assert source == "live"


class TestListModelsFallback:
    async def test_missing_key_falls_back_to_catalog(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        with patch("httpx.AsyncClient") as mock_ctor:
            models, source = await list_models("mistral")

        mock_ctor.assert_not_called()
        assert models == DEFAULT_CATALOG["mistral"]
        assert source == "catalog"

    async def test_http_error_falls_back_to_catalog(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        with patch("httpx.AsyncClient", return_value=_mock_client({}, status_code=500)):
            models, source = await list_models("mistral")

        assert models == DEFAULT_CATALOG["mistral"]
        assert source == "catalog"

    async def test_timeout_falls_back_to_catalog(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
        mock_client.aclose = AsyncMock()
        with patch("httpx.AsyncClient", return_value=mock_client):
            models, source = await list_models("mistral")

        assert models == DEFAULT_CATALOG["mistral"]
        assert source == "catalog"

    async def test_empty_live_listing_falls_back_to_catalog(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        with patch("httpx.AsyncClient", return_value=_mock_client({"data": []})):
            models, source = await list_models("mistral")

        assert models == DEFAULT_CATALOG["mistral"]
        assert source == "catalog"

    async def test_fallback_respects_catalog_overrides(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        models, source = await list_models(
            "mistral", catalog_overrides={"mistral": ["custom-model"]}
        )

        assert models == ["custom-model"]
        assert source == "catalog"

    async def test_failed_fetch_does_not_write_cache(self, monkeypatch):
        """A blip must not pin the UI to the catalog for the full TTL once the
        provider recovers -- only a successful fetch is cached."""
        monkeypatch.setenv("MISTRAL_API_KEY", "key")

        with patch("httpx.AsyncClient", return_value=_mock_client({}, status_code=500)):
            models, source = await list_models("mistral")
        assert source == "catalog"

        body = {"data": [{"id": "mistral-large"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            models, source = await list_models("mistral")
        assert models == ["mistral-large"]
        assert source == "live"


class TestListModelsCache:
    async def test_second_call_within_ttl_uses_cache_not_network(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        body = {"data": [{"id": "mistral-large"}]}
        mock_client = _mock_client(body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            first, _ = await list_models("mistral")
            second, _ = await list_models("mistral")

        assert first == second == ["mistral-large"]
        mock_client.get.assert_called_once()

    async def test_cache_expiry_triggers_a_fresh_fetch(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "key")
        body = {"data": [{"id": "mistral-large"}]}
        with patch("httpx.AsyncClient", return_value=_mock_client(body)):
            await list_models("mistral")

        # Force the cached entry to look stale.
        cached_models = model_catalog._cache["mistral"][1]
        model_catalog._cache["mistral"] = (0.0, cached_models)

        mock_client2 = _mock_client({"data": [{"id": "mistral-large"}, {"id": "mistral-new"}]})
        with patch("httpx.AsyncClient", return_value=mock_client2):
            models, source = await list_models("mistral")

        assert models == ["mistral-large", "mistral-new"]
        assert source == "live"
        mock_client2.get.assert_called_once()
