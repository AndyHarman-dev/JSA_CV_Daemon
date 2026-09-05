"""Global prompt-injection preset store (`jsa/store/injection_presets.py`) + routes
(`/api/injection-presets`).

Mirrors `test_preferences.py`'s fixture shape. Like preferences, this store always has a
valid default (an empty list), so GET never 404s and load() never returns None.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.api.routes_injection_presets import MAX_FIELD_CHARS, MAX_PRESETS
from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app
from jsa.store import injection_presets
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


def _preset(**over) -> dict:
    base = {
        "id": "p1",
        "name": "Lead with payments",
        "prefix": "Be terse.",
        "postfix": "Never hedge.",
        "first_msg": "Mention the payments work.",
        "saved_at": "2026-09-03T10:00:00Z",
    }
    base.update(over)
    return base


class TestStore:
    async def test_load_defaults_when_absent(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        path = injection_presets.injection_presets_path(settings)
        assert path == tmp_path / "injection_presets.json"
        assert not path.exists()

        loaded = await injection_presets.load(settings)
        assert loaded.presets == []

    async def test_save_then_load_roundtrip(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        stored = injection_presets.InjectionPresets(
            presets=[
                injection_presets.InjectionPreset(**_preset()),
                injection_presets.InjectionPreset(id="p2", name="Second"),
            ]
        )
        await injection_presets.save(settings, stored)
        assert injection_presets.injection_presets_path(settings).exists()

        loaded = await injection_presets.load(settings)
        assert [p.id for p in loaded.presets] == ["p1", "p2"]
        first = loaded.presets[0]
        assert first.name == "Lead with payments"
        assert first.prefix == "Be terse."
        assert first.postfix == "Never hedge."
        assert first.first_msg == "Mention the payments work."
        assert first.saved_at == "2026-09-03T10:00:00Z"
        # Optional text fields default to "" rather than None.
        assert loaded.presets[1].prefix == ""
        assert loaded.presets[1].first_msg == ""

    async def test_read_is_path_based_and_fresh(self, tmp_path):
        settings = Settings(db_path=tmp_path / "test.sqlite")
        path = injection_presets.injection_presets_path(settings)
        assert (await injection_presets.read(path)).presets == []

        await injection_presets.save(
            settings,
            injection_presets.InjectionPresets(
                presets=[injection_presets.InjectionPreset(id="p9", name="Later")]
            ),
        )
        reread = await injection_presets.read(path)
        assert [p.id for p in reread.presets] == ["p9"]

    async def test_field_name_is_snake_case_on_the_wire(self, tmp_path):
        """The prototype's camelCase `firstMsg` is wrong for this repo — every DTO is snake."""
        settings = Settings(db_path=tmp_path / "test.sqlite")
        await injection_presets.save(
            settings,
            injection_presets.InjectionPresets(
                presets=[injection_presets.InjectionPreset(id="p1", name="n", first_msg="hi")]
            ),
        )
        raw = injection_presets.injection_presets_path(settings).read_text(encoding="utf-8")
        assert '"first_msg"' in raw
        assert "firstMsg" not in raw

    async def test_store_reads_an_over_cap_file_instead_of_raising(self, tmp_path):
        """The caps are a WRITE-side guard on the route body model, deliberately NOT on the
        store models. Moving them onto ``InjectionPreset`` as a "simplification" would make an
        already-stored over-cap file (hand-edited, or written by a build with a larger cap)
        raise on load and 500 the GET forever, with no way to repair it through the API. This
        test is the gate on that decision — do not delete it along with the uncapped models."""
        settings = Settings(db_path=tmp_path / "test.sqlite")
        path = injection_presets.injection_presets_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        huge = "x" * (MAX_FIELD_CHARS + 1)
        payload = {
            "presets": [
                {"id": f"p{i}", "name": "n", "prefix": huge, "postfix": "", "first_msg": "",
                 "saved_at": ""}
                for i in range(MAX_PRESETS + 1)
            ]
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

        loaded = await injection_presets.read(path)
        assert len(loaded.presets) == MAX_PRESETS + 1
        assert loaded.presets[0].prefix == huge


class TestGetPut:
    async def test_get_returns_empty_list_never_404(self, client):
        resp = await client.get("/api/injection-presets")
        assert resp.status_code == 200
        assert resp.json() == {"presets": []}

    async def test_put_then_get(self, client):
        body = {"presets": [_preset(), _preset(id="p2", name="Second")]}
        put = await client.put("/api/injection-presets", json=body)
        assert put.status_code == 200
        assert put.json() == body

        get = await client.get("/api/injection-presets")
        assert get.status_code == 200
        assert get.json() == body

    async def test_put_whole_list_replaces_reorders_and_deletes(self, client):
        await client.put(
            "/api/injection-presets",
            json={"presets": [_preset(id="a", name="A"), _preset(id="b", name="B")]},
        )
        # A whole-list PUT is the only mutation: reorder + delete in one shot.
        put = await client.put(
            "/api/injection-presets", json={"presets": [_preset(id="b", name="B")]}
        )
        assert put.status_code == 200
        assert [p["id"] for p in (await client.get("/api/injection-presets")).json()["presets"]] == [
            "b"
        ]

    async def test_put_empty_list_clears(self, client):
        await client.put("/api/injection-presets", json={"presets": [_preset()]})
        put = await client.put("/api/injection-presets", json={"presets": []})
        assert put.status_code == 200
        assert (await client.get("/api/injection-presets")).json() == {"presets": []}

    async def test_optional_text_fields_default_to_empty(self, client):
        put = await client.put(
            "/api/injection-presets", json={"presets": [{"id": "p1", "name": "Bare"}]}
        )
        assert put.status_code == 200
        assert put.json()["presets"][0] == {
            "id": "p1",
            "name": "Bare",
            "prefix": "",
            "postfix": "",
            "first_msg": "",
            "saved_at": "",
        }


class TestCaps:
    async def test_too_many_presets_rejected_422(self, client):
        seed = {"presets": [_preset(id="keep", name="Keep")]}
        assert (await client.put("/api/injection-presets", json=seed)).status_code == 200

        over = {"presets": [_preset(id=f"p{i}") for i in range(MAX_PRESETS + 1)]}
        resp = await client.put("/api/injection-presets", json=over)
        assert resp.status_code == 422
        # Nothing persisted — the previous list survives untouched.
        assert (await client.get("/api/injection-presets")).json() == seed

    async def test_at_cap_presets_accepted(self, client):
        at_cap = {"presets": [_preset(id=f"p{i}") for i in range(MAX_PRESETS)]}
        resp = await client.put("/api/injection-presets", json=at_cap)
        assert resp.status_code == 200
        assert len((await client.get("/api/injection-presets")).json()["presets"]) == MAX_PRESETS

    @pytest.mark.parametrize("field", ["id", "name", "prefix", "postfix", "first_msg", "saved_at"])
    async def test_oversized_field_rejected_422(self, client, field):
        seed = {"presets": [_preset(id="keep", name="Keep")]}
        assert (await client.put("/api/injection-presets", json=seed)).status_code == 200

        over = {"presets": [_preset(**{field: "x" * (MAX_FIELD_CHARS + 1)})]}
        resp = await client.put("/api/injection-presets", json=over)
        assert resp.status_code == 422
        assert (await client.get("/api/injection-presets")).json() == seed

    async def test_at_cap_field_accepted(self, client):
        body = {"presets": [_preset(prefix="x" * MAX_FIELD_CHARS)]}
        resp = await client.put("/api/injection-presets", json=body)
        assert resp.status_code == 200
        got = (await client.get("/api/injection-presets")).json()["presets"][0]
        assert len(got["prefix"]) == MAX_FIELD_CHARS

    async def test_missing_required_id_rejected_422(self, client):
        resp = await client.put("/api/injection-presets", json={"presets": [{"name": "No id"}]})
        assert resp.status_code == 422
