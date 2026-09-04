"""Phase 2 — CV decks HTTP API (`jsa/api/routes_cv_decks.py`) + config signal.

Covers:
- Full CRUD happy paths for `/api/cv-decks` (create, list, get, put, patch rename,
  patch set-default, duplicate, delete) plus 404/400/422 error mapping.
- `orchestrator.kick()` fires on PUT, PATCH-default, and DELETE — not on plain create,
  rename, or duplicate.
- `GET /api/cv-decks` is a single index read — it must never open a deck file
  (`jsa.store.cv_structure.read` is never called).
- Legacy-alias equivalence: `PUT /api/cv-structure` creates exactly one deck, visible via
  both `GET /api/cv-decks` and `GET /api/cv-structure`.
- `cv_structure_exists` (`GET /api/config`) toggles false -> true -> false across
  create/save/delete, and `cv_deck_count` tracks the number of decks.

Uses a tmp_path DB so `Settings.cv_decks_path`/`cv_decks_dir` are isolated per test,
mirroring `test_cv_structure.py`'s fixture shape exactly.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.config import Settings
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app
from jsa.store import cv_structure
from tests.backend.fakes.fake_backend import FakeAgentBackend

_VALID_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com", "location": "Berlin",
                "links": ["github.com/janedoe"]},
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"heading": "Senior Engineer", "subheading": "Acme", "dates": "2020–Present",
             "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker"]},
    ],
}

_OTHER_CV = {
    "contact": {"name": "John Smith", "email": "john@x.com"},
    "sections": [{"name": "Summary", "text": "Data engineer."}],
}

# A cover letter wearing CV shape — must trip CVDocument._not_a_cover_letter (≥2 formulas).
_COVER_LETTER_AS_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the role.",
        "I would welcome discussing it further. Sincerely, Jane",
    ]}],
}


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


# --- CRUD happy paths --------------------------------------------------------------------


class TestCrudHappyPath:
    async def test_full_lifecycle(self, client):
        # Fresh install: no decks yet.
        listed = await client.get("/api/cv-decks")
        assert listed.status_code == 200
        assert listed.json() == {"decks": [], "default_id": None}

        # Create a new, empty deck slot — becomes the default automatically (first deck).
        created = await client.post("/api/cv-decks", json={"name": "Backend"})
        assert created.status_code == 201
        deck = created.json()["deck"]
        deck_id = deck["id"]
        assert deck["name"] == "Backend"
        assert deck["auto_title"] is None
        assert deck["has_cv"] is False
        assert deck["is_default"] is True

        # GET on an empty slot -> 404.
        empty_get = await client.get(f"/api/cv-decks/{deck_id}")
        assert empty_get.status_code == 404

        # PUT persists a CV into it.
        put = await client.put(f"/api/cv-decks/{deck_id}", json={"structured": _VALID_CV})
        assert put.status_code == 200
        assert put.json()["structured"]["contact"]["name"] == "Jane Doe"

        # GET now returns the structured content.
        got = await client.get(f"/api/cv-decks/{deck_id}")
        assert got.status_code == 200
        assert got.json()["structured"]["contact"]["name"] == "Jane Doe"

        # The index reflects has_cv + the refreshed auto_title.
        listed2 = await client.get("/api/cv-decks")
        assert listed2.status_code == 200
        (meta,) = listed2.json()["decks"]
        assert meta["has_cv"] is True
        assert meta["auto_title"] == "Jane Doe"
        assert listed2.json()["default_id"] == deck_id

        # PATCH renames it.
        renamed = await client.patch(f"/api/cv-decks/{deck_id}", json={"name": "Backend v2"})
        assert renamed.status_code == 200
        assert renamed.json()["deck"]["name"] == "Backend v2"

        # Create a second deck and make it the default via PATCH is_default.
        second = await client.post("/api/cv-decks", json={"name": None})
        second_id = second.json()["deck"]["id"]
        assert second.json()["deck"]["is_default"] is False  # first deck is still default

        set_default = await client.patch(
            f"/api/cv-decks/{second_id}", json={"is_default": True}
        )
        assert set_default.status_code == 200
        assert set_default.json()["deck"]["is_default"] is True

        listed3 = await client.get("/api/cv-decks")
        assert listed3.json()["default_id"] == second_id

        # Duplicate the first (now non-default) deck.
        dup = await client.post(f"/api/cv-decks/{deck_id}/duplicate", json={"name": "Copy"})
        assert dup.status_code == 201
        dup_deck = dup.json()["deck"]
        assert dup_deck["id"] != deck_id
        assert dup_deck["name"] == "Copy"
        assert dup_deck["has_cv"] is True
        dup_get = await client.get(f"/api/cv-decks/{dup_deck['id']}")
        assert dup_get.json()["structured"]["contact"]["name"] == "Jane Doe"

        # Delete the duplicate.
        deleted = await client.delete(f"/api/cv-decks/{dup_deck['id']}")
        assert deleted.status_code == 204

        listed4 = await client.get("/api/cv-decks")
        assert {d["id"] for d in listed4.json()["decks"]} == {deck_id, second_id}


# --- error mapping ------------------------------------------------------------------------


class TestErrorMapping:
    async def test_put_invalid_structure_422(self, client):
        created = await client.post("/api/cv-decks", json={"name": None})
        deck_id = created.json()["deck"]["id"]

        resp = await client.put(
            f"/api/cv-decks/{deck_id}", json={"structured": _COVER_LETTER_AS_CV}
        )
        assert resp.status_code == 422
        assert "cover letter" in resp.json()["detail"].lower()
        # Nothing was persisted — deck stays empty.
        assert (await client.get(f"/api/cv-decks/{deck_id}")).status_code == 404

    async def test_unknown_deck_id_404(self, client):
        unknown = "a" * 32
        assert (await client.get(f"/api/cv-decks/{unknown}")).status_code == 404
        put = await client.put(f"/api/cv-decks/{unknown}", json={"structured": _VALID_CV})
        assert put.status_code == 404
        patch = await client.patch(f"/api/cv-decks/{unknown}", json={"name": "x"})
        assert patch.status_code == 404
        dup = await client.post(f"/api/cv-decks/{unknown}/duplicate", json={"name": None})
        assert dup.status_code == 404

    async def test_malformed_deck_id_400(self, client):
        # Not a slash-containing traversal (that never reaches our route — the ASGI/HTTP
        # layer collapses it before dispatch); a same-length non-hex string exercises the
        # store's own format guard (`_DECK_ID_RE`) directly.
        bad = "not-a-valid-deck-id-zzzzzzzzzzzz"
        assert (await client.get(f"/api/cv-decks/{bad}")).status_code == 400
        put = await client.put(f"/api/cv-decks/{bad}", json={"structured": _VALID_CV})
        assert put.status_code == 400
        patch = await client.patch(f"/api/cv-decks/{bad}", json={"name": "x"})
        assert patch.status_code == 400
        dup = await client.post(f"/api/cv-decks/{bad}/duplicate", json={"name": None})
        assert dup.status_code == 400
        delete = await client.delete(f"/api/cv-decks/{bad}")
        assert delete.status_code == 400

    async def test_delete_unknown_deck_id_is_a_noop_204(self, client):
        """DELETE is idempotent — a well-formed but absent id still returns 204."""
        unknown = "b" * 32
        resp = await client.delete(f"/api/cv-decks/{unknown}")
        assert resp.status_code == 204


# --- kick() wiring -------------------------------------------------------------------------


class TestKickWiring:
    async def test_put_kicks(self, client, monkeypatch):
        created = await client.post("/api/cv-decks", json={"name": None})
        deck_id = created.json()["deck"]["id"]

        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.put(f"/api/cv-decks/{deck_id}", json={"structured": _VALID_CV})
        assert resp.status_code == 200
        assert calls == ["kick"]

    async def test_patch_set_default_kicks(self, client, monkeypatch):
        first = await client.post("/api/cv-decks", json={"name": None})
        first_id = first.json()["deck"]["id"]
        second = await client.post("/api/cv-decks", json={"name": None})
        second_id = second.json()["deck"]["id"]
        assert first_id  # first deck is the implicit default already

        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.patch(f"/api/cv-decks/{second_id}", json={"is_default": True})
        assert resp.status_code == 200
        assert calls == ["kick"]

    async def test_patch_rename_only_does_not_kick(self, client, monkeypatch):
        created = await client.post("/api/cv-decks", json={"name": None})
        deck_id = created.json()["deck"]["id"]

        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.patch(f"/api/cv-decks/{deck_id}", json={"name": "renamed"})
        assert resp.status_code == 200
        assert calls == []

    async def test_delete_kicks(self, client, monkeypatch):
        created = await client.post("/api/cv-decks", json={"name": None})
        deck_id = created.json()["deck"]["id"]

        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.delete(f"/api/cv-decks/{deck_id}")
        assert resp.status_code == 204
        assert calls == ["kick"]

    async def test_create_does_not_kick(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append("kick"))
        resp = await client.post("/api/cv-decks", json={"name": None})
        assert resp.status_code == 201
        assert calls == []


# --- GET /api/cv-decks is a single index read -----------------------------------------------


class TestListIsSingleIndexRead:
    async def test_list_never_opens_a_deck_file(self, client, monkeypatch):
        # Seed a couple of decks with real CVs first (outside the patched window).
        d1 = await client.post("/api/cv-decks", json={"name": None})
        await client.put(
            f"/api/cv-decks/{d1.json()['deck']['id']}", json={"structured": _VALID_CV}
        )
        d2 = await client.post("/api/cv-decks", json={"name": None})
        await client.put(
            f"/api/cv-decks/{d2.json()['deck']['id']}", json={"structured": _OTHER_CV}
        )

        calls = []
        original_read = cv_structure.read

        async def _tracking_read(path):
            calls.append(path)
            return await original_read(path)

        monkeypatch.setattr(cv_structure, "read", _tracking_read)

        resp = await client.get("/api/cv-decks")
        assert resp.status_code == 200
        assert len(resp.json()["decks"]) == 2
        assert calls == [], "GET /api/cv-decks must never read a deck file"


# --- legacy alias equivalence ----------------------------------------------------------------


class TestLegacyAliasEquivalence:
    async def test_put_cv_structure_creates_one_deck_visible_both_ways(self, client):
        put = await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        assert put.status_code == 200

        listed = await client.get("/api/cv-decks")
        assert listed.status_code == 200
        (deck,) = listed.json()["decks"]
        assert deck["has_cv"] is True
        assert deck["is_default"] is True
        assert deck["auto_title"] == "Jane Doe"

        got = await client.get("/api/cv-structure")
        assert got.status_code == 200
        assert got.json()["structured"]["contact"]["name"] == "Jane Doe"

    async def test_get_cv_structure_404_when_no_decks(self, client):
        resp = await client.get("/api/cv-structure")
        assert resp.status_code == 404

    async def test_put_cv_structure_reuses_existing_default_deck(self, client):
        """A second PUT must overwrite the same default deck, not create a new one."""
        await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        await client.put("/api/cv-structure", json={"structured": _OTHER_CV})

        listed = await client.get("/api/cv-decks")
        assert len(listed.json()["decks"]) == 1
        assert listed.json()["decks"][0]["auto_title"] == "John Smith"

    async def test_put_cv_structure_kicks_orchestrator(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(Orchestrator, "kick", lambda self: calls.append(True))
        resp = await client.put("/api/cv-structure", json={"structured": _VALID_CV})
        assert resp.status_code == 200
        assert calls == [True]


# --- cv_structure_exists / cv_deck_count -----------------------------------------------------


class TestConfigSignal:
    async def test_toggles_false_true_false_across_create_save_delete(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert resp.json()["cv_structure_exists"] is False
        assert resp.json()["cv_deck_count"] == 0

        # An empty deck slot alone must NOT flip the signal (has_cv=False).
        created = await client.post("/api/cv-decks", json={"name": None})
        deck_id = created.json()["deck"]["id"]
        resp2 = await client.get("/api/config")
        assert resp2.json()["cv_structure_exists"] is False
        assert resp2.json()["cv_deck_count"] == 1

        # Saving a CV into it flips the signal true.
        await client.put(f"/api/cv-decks/{deck_id}", json={"structured": _VALID_CV})
        resp3 = await client.get("/api/config")
        assert resp3.json()["cv_structure_exists"] is True
        assert resp3.json()["cv_deck_count"] == 1

        # Deleting the only deck flips it back false.
        await client.delete(f"/api/cv-decks/{deck_id}")
        resp4 = await client.get("/api/config")
        assert resp4.json()["cv_structure_exists"] is False
        assert resp4.json()["cv_deck_count"] == 0
