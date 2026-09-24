"""CV-editor AI chat plan, Phase 3: ``jsa/api/routes_cv_chat.py`` HTTP surface.

Uses the same ``test_app``/``client`` fixture shape as ``test_cv_decks_api.py``
(``Orchestrator.run`` neutered, ``AsyncClient`` + ``ASGITransport``, inside
``lifespan_context``), overriding ``test_app.state.backend_factory`` POST-startup so
each test scripts its own reply."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from jsa.config import Settings
from jsa.events.bus import bus
from jsa.pipeline.orchestrator import Orchestrator
from jsa.server import create_app
from tests.backend.fakes.fake_backend import FakeAgentBackend

_VALID_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {
            "name": "Experience",
            "entries": [
                {"heading": "Senior Engineer", "subheading": "Acme", "bullets": ["Did X"]},
            ],
        },
    ],
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


async def _create_deck_with_cv(client) -> str:
    created = await client.post("/api/cv-decks", json={"name": None})
    deck_id = created.json()["deck"]["id"]
    await client.put(f"/api/cv-decks/{deck_id}", json={"structured": _VALID_CV})
    return deck_id


def _script(test_app, replies: list) -> FakeAgentBackend:
    backend = FakeAgentBackend(replies)
    test_app.state.backend_factory = lambda name: backend
    return backend


def _final_reply(answer: str, ops: list[dict]) -> dict:
    body = json.dumps({"answer": answer, "ops": ops})
    return {"raw": f"<<<FINAL>>>\n{body}\n<<<END>>>", "content": body, "kind": "final"}


def _summary_op(text: str) -> dict:
    return {
        "op": "replace_summary", "section_id": None, "entry_id": None, "position": None,
        "text": text, "bullets": None, "order": None, "section": None, "entry": None,
        "contact": None,
    }


def _payload(scope: dict, cv: dict, *, instruction: str | None = None,
             quick_action: str | None = None, base_hash: str = "h1") -> dict:
    return {
        "scope": scope,
        "instruction": instruction,
        "quick_action": quick_action,
        "cv": cv,
        "base_hash": base_hash,
    }


class TestFullRoundTrip:
    async def test_post_get_delete(self, test_app, client):
        from jsa.agents.base import AgentReply
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [AgentReply(**_final_reply("Tightened.", [_summary_op("New text.")]))])

        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "section", "section_index": 0}, _VALID_CV, instruction="tighten it",
            ))},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["turns"]) == 2
        assert body["turns"][1]["document"]["sections"][0]["text"] == "New text."

        got = await client.get(f"/api/cv-decks/{deck_id}/chat")
        assert len(got.json()["turns"]) == 2

        deleted = await client.delete(f"/api/cv-decks/{deck_id}/chat")
        assert deleted.status_code == 204
        got2 = await client.get(f"/api/cv-decks/{deck_id}/chat")
        assert got2.json()["turns"] == []


class TestErrorMapping:
    async def test_malformed_deck_id_400(self, client):
        resp = await client.get("/api/cv-decks/not-a-valid-id/chat")
        assert resp.status_code == 400

    async def test_unknown_deck_404(self, client):
        resp = await client.get(f"/api/cv-decks/{'a' * 32}/chat")
        assert resp.status_code == 404

    async def test_bad_model_reply_422(self, test_app, client):
        from jsa.agents.base import AgentReply
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [AgentReply(raw="not sentinel-wrapped", content="oops", kind="final")])

        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "cv"}, _VALID_CV, instruction="do something",
            ))},
        )
        assert resp.status_code == 422

    async def test_missing_instruction_and_quick_action_422(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload({"type": "cv"}, _VALID_CV))},
        )
        assert resp.status_code == 422

    async def test_section_scope_missing_index_422(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "section"}, _VALID_CV, instruction="x",
            ))},
        )
        assert resp.status_code == 422

    async def test_section_scope_out_of_range_index_422(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "section", "section_index": 99}, _VALID_CV, instruction="x",
            ))},
        )
        assert resp.status_code == 422

    async def test_section_scope_negative_index_422(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "section", "section_index": -1}, _VALID_CV, instruction="x",
            ))},
        )
        assert resp.status_code == 422

    async def test_entry_scope_out_of_range_entry_index_422(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "entry", "section_index": 1, "entry_index": 99},
                _VALID_CV, instruction="x",
            ))},
        )
        assert resp.status_code == 422


class TestAttachments:
    async def test_txt_attachment_is_accepted(self, test_app, client):
        from jsa.agents.base import AgentReply
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [AgentReply(**_final_reply("Updated from attachment.", []))])

        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "contact"}, _VALID_CV, instruction="fill in identity",
            ))},
            files={"files": ("notes.txt", b"Jane Doe, jane@x.com", "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["turns"][0]["files"] == [{"name": "notes.txt", "size": 20}]

    async def test_png_attachment_rejected(self, test_app, client):
        deck_id = await _create_deck_with_cv(client)
        _script(test_app, [])
        resp = await client.post(
            f"/api/cv-decks/{deck_id}/chat",
            data={"payload": json.dumps(_payload(
                {"type": "contact"}, _VALID_CV, instruction="fill in identity",
            ))},
            files={"files": ("photo.png", b"\x89PNG\r\n", "image/png")},
        )
        assert resp.status_code == 422


class TestThreadPersistence:
    async def test_persists_across_a_fresh_client(self, test_app, tmp_path):
        from jsa.agents.base import AgentReply

        async with test_app.router.lifespan_context(test_app):
            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as ac:
                deck_id = await _create_deck_with_cv(ac)
                _script(test_app, [AgentReply(**_final_reply("Done.", [_summary_op("X.")]))])
                await ac.post(
                    f"/api/cv-decks/{deck_id}/chat",
                    data={"payload": json.dumps(_payload(
                        {"type": "section", "section_index": 0}, _VALID_CV,
                        instruction="x",
                    ))},
                )

            async with AsyncClient(
                transport=ASGITransport(app=test_app), base_url="http://test"
            ) as ac2:
                got = await ac2.get(f"/api/cv-decks/{deck_id}/chat")
                assert len(got.json()["turns"]) == 2


class TestBusEvents:
    async def test_chat_chunk_and_turn_end_reach_the_bus(self, test_app, client):
        from jsa.agents.base import AgentChunk, AgentReply

        deck_id = await _create_deck_with_cv(client)
        backend = FakeAgentBackend(
            [AgentReply(**_final_reply("Done.", [_summary_op("X.")]))],
            supports_streaming=True,
            scripted_chunks=[[AgentChunk(kind="reasoning", text="thinking")]],
        )
        test_app.state.backend_factory = lambda name: backend

        queue = bus.subscribe()
        try:
            resp = await client.post(
                f"/api/cv-decks/{deck_id}/chat",
                data={"payload": json.dumps(_payload(
                    {"type": "section", "section_index": 0}, _VALID_CV, instruction="x",
                ))},
            )
            assert resp.status_code == 200, resp.text
            types = set()
            while not queue.empty():
                types.add(queue.get_nowait()["type"])
            assert "chat_chunk" in types
            assert "chat_turn_end" in types
        finally:
            bus.unsubscribe(queue)
