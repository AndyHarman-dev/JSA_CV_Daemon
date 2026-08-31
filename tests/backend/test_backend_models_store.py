"""Per-backend model selection store (`jsa/store/backend_models.py`) + routes
(`/api/backend-models`). Mirrors `test_preferences.py`'s fixture shape.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app
from jsa.store import backend_models
from tests.backend.fakes.fake_backend import FakeAgentBackend


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name, **kwargs: FakeAgentBackend([]))

    settings = Settings(
        output_dir=tmp_path / "output",
        db_path=tmp_path / "test.sqlite",
        backend="claude-cli",
        no_browser=True,
    )
    return create_app(settings)


@pytest.fixture
async def client(test_app):
    async with test_app.router.lifespan_context(test_app):
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as ac:
            yield ac


class TestStore:
    async def test_load_defaults_when_absent(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        assert backend_models.backend_models_path(settings) == tmp_path / "backend_models.json"
        models = await backend_models.load(settings)
        assert models.selected == {}
        assert models.catalog == {}

    async def test_save_then_load_roundtrip(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        await backend_models.save(
            settings, backend_models.BackendModels(selected={"anthropic": "claude-opus-5"})
        )
        assert backend_models.backend_models_path(settings).exists()
        loaded = await backend_models.load(settings)
        assert loaded.selected == {"anthropic": "claude-opus-5"}

    async def test_read_is_path_based_and_fresh(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        path = backend_models.backend_models_path(settings)
        assert (await backend_models.read(path)).selected == {}
        await backend_models.save(
            settings, backend_models.BackendModels(selected={"claude-cli": "claude-sonnet-5"})
        )
        assert (await backend_models.read(path)).selected == {"claude-cli": "claude-sonnet-5"}


class TestGet:
    async def test_get_returns_empty_selection_and_support_flags(self, client):
        resp = await client.get("/api/backend-models")
        assert resp.status_code == 200
        body = resp.json()
        assert body["selected"] == {}
        assert body["supports_model_selection"]["google-cli"] is False
        assert body["supports_model_selection"]["anthropic"] is True

    async def test_get_per_backend_returns_catalog(self, client):
        resp = await client.get("/api/backend-models/anthropic")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == "anthropic"
        assert "claude-haiku-4-5" in body["models"]
        assert body["selected"] is None
        assert body["source"] == "catalog"

    async def test_get_per_backend_google_cli_has_empty_models(self, client):
        resp = await client.get("/api/backend-models/google-cli")
        assert resp.status_code == 200
        assert resp.json()["models"] == []

    async def test_get_per_backend_unknown_backend_404s(self, client):
        resp = await client.get("/api/backend-models/harry-potter")
        assert resp.status_code == 404


class TestPut:
    async def test_put_valid_persists_and_get_returns(self, client):
        put = await client.put(
            "/api/backend-models", json={"backend": "anthropic", "model": "claude-opus-5"}
        )
        assert put.status_code == 200
        assert put.json() == {"backend": "anthropic", "model": "claude-opus-5"}

        get = await client.get("/api/backend-models")
        assert get.json()["selected"] == {"anthropic": "claude-opus-5"}

        get_one = await client.get("/api/backend-models/anthropic")
        assert get_one.json()["selected"] == "claude-opus-5"

    async def test_put_mutates_live_settings_for_next_dispatch(self, client, test_app):
        await client.put(
            "/api/backend-models", json={"backend": "opencode-zen", "model": "mimo-v2.5-free"}
        )
        assert test_app.state.settings.backend_models["opencode-zen"] == "mimo-v2.5-free"

    async def test_put_unknown_backend_422(self, client):
        resp = await client.put(
            "/api/backend-models", json={"backend": "harry-potter", "model": "x"}
        )
        assert resp.status_code == 422
        assert (await client.get("/api/backend-models")).json()["selected"] == {}

    async def test_put_google_cli_422(self, client):
        resp = await client.put(
            "/api/backend-models", json={"backend": "google-cli", "model": "x"}
        )
        assert resp.status_code == 422
        assert (await client.get("/api/backend-models")).json()["selected"] == {}


class TestStartupHydration:
    """A selection saved in a previous run must be honored from the very first dispatch
    of the next run -- not just live-mutated by a PUT within the same process."""

    async def test_saved_selection_is_hydrated_into_settings_at_startup(self, tmp_path, monkeypatch):
        async def _noop(self):
            return

        monkeypatch.setattr(Orchestrator, "run", _noop)
        monkeypatch.setattr("jsa.server.backend_for", lambda name, **kwargs: FakeAgentBackend([]))

        settings = Settings(
            output_dir=tmp_path / "output",
            db_path=tmp_path / "test.sqlite",
            backend="claude-cli",
            no_browser=True,
        )
        # Write the persisted selection to disk *before* the app's startup event runs.
        await backend_models.save(
            settings, backend_models.BackendModels(selected={"anthropic": "claude-opus-5"})
        )

        app = create_app(settings)
        async with app.router.lifespan_context(app):
            assert app.state.settings.backend_models["anthropic"] == "claude-opus-5"
