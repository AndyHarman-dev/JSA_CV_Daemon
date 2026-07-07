"""Global preferences store (`jsa/store/preferences.py`) + routes (`/api/preferences`).

Mirrors `test_cv_structure.py`'s fixture shape, with one behavioral difference: preferences
always have a valid default, so GET never 404s and load() never returns None.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app
from jsa.store import preferences
from tests.backend.fakes.fake_backend import FakeAgentBackend


@pytest.fixture
async def test_app(tmp_path, monkeypatch):
    async def _noop(self):
        return

    monkeypatch.setattr(Orchestrator, "run", _noop)
    monkeypatch.setattr("jsa.server.backend_for", lambda name: FakeAgentBackend([]))

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
        assert preferences.preferences_path(settings) == tmp_path / "preferences.json"
        prefs = await preferences.load(settings)
        assert prefs.language == "en"

    async def test_save_then_load_roundtrip(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        await preferences.save(settings, preferences.Preferences(language="es"))
        assert preferences.preferences_path(settings).exists()
        loaded = await preferences.load(settings)
        assert loaded.language == "es"

    async def test_read_is_path_based_and_fresh(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        path = preferences.preferences_path(settings)
        assert (await preferences.read(path)).language == "en"
        await preferences.save(settings, preferences.Preferences(language="fr"))
        assert (await preferences.read(path)).language == "fr"


class TestGetPut:
    async def test_get_returns_default_never_404(self, client):
        resp = await client.get("/api/preferences")
        assert resp.status_code == 200
        assert resp.json() == {"language": "en"}

    async def test_put_valid_persists_and_get_returns(self, client):
        put = await client.put("/api/preferences", json={"language": "ja"})
        assert put.status_code == 200
        assert put.json() == {"language": "ja"}

        get = await client.get("/api/preferences")
        assert get.json() == {"language": "ja"}

    async def test_put_unknown_code_rejected_422(self, client):
        resp = await client.put("/api/preferences", json={"language": "xx"})
        assert resp.status_code == 422
        # Nothing persisted.
        assert (await client.get("/api/preferences")).json() == {"language": "en"}


class TestConfigIncludesLanguages:
    async def test_config_has_languages_list(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "languages" in body
        codes = [row[0] for row in body["languages"]]
        assert "en" in codes and "es" in codes
