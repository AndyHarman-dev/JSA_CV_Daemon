"""Phase 1 — CV-decks store (`jsa/store/cv_decks.py`).

Covers the many-decks store in isolation (no HTTP, no pipeline): config paths, index
round-trips, deck CRUD, the `deck_path` path-traversal guard, `resolve_path`'s fallback
order, and legacy-install migration. Mirrors `tests/backend/test_cv_structure.py`'s shape
(a `tmp_path`-derived `Settings` auto-isolates every test).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jsa.config import Settings
from jsa.schema import CVDocument
from jsa.store import cv_decks, cv_structure

_VALID_CV = {
    "contact": {
        "name": "Jane Doe", "email": "jane@x.com", "location": "Berlin",
        "links": ["github.com/janedoe"],
    },
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"heading": "Senior Engineer", "subheading": "Acme", "dates": "2020-Present",
             "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker"]},
    ],
}

_OTHER_CV = {
    "contact": {"name": "John Smith", "email": "john@x.com"},
    "sections": [{"name": "Summary", "text": "Frontend engineer."}],
}


def _settings(tmp_path) -> Settings:
    return Settings(db_path=tmp_path / "jsa.sqlite")


# --- config paths -------------------------------------------------------------------------


class TestConfigPaths:
    def test_cv_decks_path_derived_from_db_path(self, tmp_path):
        settings = _settings(tmp_path)
        assert settings.cv_decks_path == tmp_path / "cv_decks.json"

    def test_cv_decks_dir_derived_from_db_path(self, tmp_path):
        settings = _settings(tmp_path)
        assert settings.cv_decks_dir == tmp_path / "cv_decks"


# --- deck_path path-traversal guard ---------------------------------------------------------


class TestDeckPath:
    def test_rejects_path_traversal(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.InvalidDeckId):
            cv_decks.deck_path(settings, "../x")

    def test_rejects_empty_string(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.InvalidDeckId):
            cv_decks.deck_path(settings, "")

    def test_rejects_non_hex_id(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.InvalidDeckId):
            cv_decks.deck_path(settings, "not-a-valid-uuid-hex")

    def test_invalid_deck_id_is_a_value_error(self, tmp_path):
        """Existing callers that only catch ValueError must keep working unchanged."""
        settings = _settings(tmp_path)
        with pytest.raises(ValueError):
            cv_decks.deck_path(settings, "../x")

    def test_accepts_valid_32_char_hex_id(self, tmp_path):
        settings = _settings(tmp_path)
        deck_id = "0123456789abcdef0123456789abcdef"
        path = cv_decks.deck_path(settings, deck_id)
        assert path == settings.cv_decks_dir / f"{deck_id}.json"


# --- index defaults / read_index -------------------------------------------------------------


class TestIndexDefaults:
    async def test_read_index_absent_file_returns_default_instance(self, tmp_path):
        missing = tmp_path / "does-not-exist.json"
        index = await cv_decks.read_index(missing)
        assert index == cv_decks.DeckIndex()
        assert index.decks == []
        assert index.default_id is None

    async def test_load_index_empty_install_returns_default_and_writes_nothing(self, tmp_path):
        settings = _settings(tmp_path)
        index = await cv_decks.load_index(settings)
        assert index == cv_decks.DeckIndex()
        assert not settings.cv_decks_path.exists(), "fresh install must not write an index file"


# --- create / save / load round trip ----------------------------------------------------------


class TestCreateSaveLoad:
    async def test_create_deck_has_no_cv_and_becomes_default(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings, name="My Deck")
        assert meta.has_cv is False
        assert meta.auto_title is None
        assert meta.name == "My Deck"

        index = await cv_decks.load_index(settings)
        assert index.default_id == meta.id
        assert [m.id for m in index.decks] == [meta.id]

    async def test_second_created_deck_does_not_become_default(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        second = await cv_decks.create_deck(settings)

        index = await cv_decks.load_index(settings)
        assert index.default_id == first.id
        assert [m.id for m in index.decks] == [first.id, second.id]

    async def test_save_deck_writes_file_and_refreshes_auto_title(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        cv = CVDocument.model_validate(_VALID_CV)

        await cv_decks.save_deck(settings, meta.id, cv)

        assert cv_decks.deck_path(settings, meta.id).exists()
        index = await cv_decks.load_index(settings)
        saved_meta = next(m for m in index.decks if m.id == meta.id)
        assert saved_meta.has_cv is True
        assert saved_meta.auto_title == "Jane Doe"

    async def test_save_deck_unknown_id_raises_and_leaves_no_orphan_file(self, tmp_path):
        settings = _settings(tmp_path)
        deck_id = "0123456789abcdef0123456789abcdef"
        cv = CVDocument.model_validate(_VALID_CV)
        with pytest.raises(cv_decks.UnknownDeckId):
            await cv_decks.save_deck(settings, deck_id, cv)
        assert not cv_decks.deck_path(settings, deck_id).exists()

    async def test_load_deck_roundtrip(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        cv = CVDocument.model_validate(_VALID_CV)
        await cv_decks.save_deck(settings, meta.id, cv)

        loaded = await cv_decks.load_deck(settings, meta.id)
        assert loaded is not None
        assert loaded.contact.name == "Jane Doe"
        assert [s.name for s in loaded.sections] == ["Summary", "Experience", "Skills"]

    async def test_load_deck_never_saved_returns_none(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        assert await cv_decks.load_deck(settings, meta.id) is None

    async def test_auto_title_refreshed_on_resave(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_VALID_CV))
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_OTHER_CV))

        index = await cv_decks.load_index(settings)
        saved_meta = next(m for m in index.decks if m.id == meta.id)
        assert saved_meta.auto_title == "John Smith"


# --- rename / set_default ------------------------------------------------------------------


class TestRenameSetDefault:
    async def test_rename_deck(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings, name="Original")
        renamed = await cv_decks.rename_deck(settings, meta.id, "Renamed")
        assert renamed.name == "Renamed"

        index = await cv_decks.load_index(settings)
        assert next(m for m in index.decks if m.id == meta.id).name == "Renamed"

    async def test_rename_unknown_deck_raises(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.UnknownDeckId):
            await cv_decks.rename_deck(settings, "0123456789abcdef0123456789abcdef", "x")

    async def test_set_default_switches_default_id(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        second = await cv_decks.create_deck(settings)

        await cv_decks.set_default(settings, second.id)

        index = await cv_decks.load_index(settings)
        assert index.default_id == second.id
        assert index.default_id != first.id

    async def test_set_default_unknown_deck_raises(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.UnknownDeckId):
            await cv_decks.set_default(settings, "0123456789abcdef0123456789abcdef")


# --- duplicate ------------------------------------------------------------------------------


class TestDuplicate:
    async def test_duplicate_copies_source_file_content(self, tmp_path):
        settings = _settings(tmp_path)
        src = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, src.id, CVDocument.model_validate(_VALID_CV))

        dup = await cv_decks.duplicate_deck(settings, src.id, name="Copy")
        assert dup.id != src.id
        assert dup.name == "Copy"
        assert dup.has_cv is True

        loaded = await cv_decks.load_deck(settings, dup.id)
        assert loaded is not None
        assert loaded.contact.name == "Jane Doe"

        index = await cv_decks.load_index(settings)
        assert {m.id for m in index.decks} == {src.id, dup.id}

    async def test_duplicate_empty_deck_slot_has_no_cv(self, tmp_path):
        settings = _settings(tmp_path)
        src = await cv_decks.create_deck(settings)  # never saved

        dup = await cv_decks.duplicate_deck(settings, src.id, name="Copy")
        assert dup.has_cv is False
        assert not cv_decks.deck_path(settings, dup.id).exists()

    async def test_duplicate_unknown_source_raises(self, tmp_path):
        settings = _settings(tmp_path)
        with pytest.raises(cv_decks.UnknownDeckId):
            await cv_decks.duplicate_deck(
                settings, "0123456789abcdef0123456789abcdef", name="x"
            )

    async def test_unknown_deck_id_is_a_value_error(self, tmp_path):
        """Existing callers that only catch ValueError must keep working unchanged."""
        settings = _settings(tmp_path)
        with pytest.raises(ValueError):
            await cv_decks.duplicate_deck(
                settings, "0123456789abcdef0123456789abcdef", name="x"
            )


# --- create from an existing CV (a job's tailored CV -> new base CV) -------------------------


class TestCreateDeckFromCv:
    async def test_creates_a_filled_deck_with_name_and_auto_title(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck_from_cv(
            settings, CVDocument.model_validate(_VALID_CV), name="Acme · Engineer"
        )
        assert meta.name == "Acme · Engineer"
        assert meta.auto_title == "Jane Doe"
        assert meta.has_cv is True

        loaded = await cv_decks.load_deck(settings, meta.id)
        assert loaded == CVDocument.model_validate(_VALID_CV)

        index = await cv_decks.load_index(settings)
        assert [m.id for m in index.decks] == [meta.id]
        assert index.decks[0].has_cv is True

    async def test_does_not_take_over_an_existing_default(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, first.id, CVDocument.model_validate(_OTHER_CV))

        meta = await cv_decks.create_deck_from_cv(
            settings, CVDocument.model_validate(_VALID_CV), name="x"
        )

        index = await cv_decks.load_index(settings)
        assert index.default_id == first.id
        assert [m.id for m in index.decks] == [first.id, meta.id]

    async def test_first_deck_becomes_default(self, tmp_path):
        """`ensure_default_deck` relies on `default_id is None` meaning "no decks", so the
        first deck must claim the default no matter which path created it."""
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck_from_cv(
            settings, CVDocument.model_validate(_VALID_CV)
        )
        assert (await cv_decks.load_index(settings)).default_id == meta.id
        assert await cv_decks.ensure_default_deck(settings) == meta.id

    async def test_failed_write_leaves_no_index_entry(self, tmp_path, monkeypatch):
        """The reason this is one function and not create_deck + save_deck: a write that
        fails must not strand an empty slot in the rail."""
        settings = _settings(tmp_path)

        async def _boom(path, cv):
            raise OSError("disk full")

        monkeypatch.setattr(cv_structure, "write", _boom)
        with pytest.raises(OSError):
            await cv_decks.create_deck_from_cv(
                settings, CVDocument.model_validate(_VALID_CV), name="x"
            )
        assert (await cv_decks.load_index(settings)).decks == []


# --- delete -----------------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_file_and_index_entry(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_VALID_CV))

        await cv_decks.delete_deck(settings, meta.id)

        assert not cv_decks.deck_path(settings, meta.id).exists()
        index = await cv_decks.load_index(settings)
        assert index.decks == []
        assert index.default_id is None

    async def test_delete_promotes_next_deck_to_default(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        second = await cv_decks.create_deck(settings)
        assert (await cv_decks.load_index(settings)).default_id == first.id

        await cv_decks.delete_deck(settings, first.id)

        index = await cv_decks.load_index(settings)
        assert index.default_id == second.id
        assert [m.id for m in index.decks] == [second.id]

    async def test_delete_non_default_deck_keeps_default(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        second = await cv_decks.create_deck(settings)

        await cv_decks.delete_deck(settings, second.id)

        index = await cv_decks.load_index(settings)
        assert index.default_id == first.id
        assert [m.id for m in index.decks] == [first.id]

    async def test_delete_missing_file_is_a_noop_on_disk(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)  # has_cv=False, no file
        await cv_decks.delete_deck(settings, meta.id)  # must not raise
        index = await cv_decks.load_index(settings)
        assert index.decks == []


# --- resolve_path -------------------------------------------------------------------------------


class TestResolvePath:
    async def test_resolves_requested_deck_when_it_has_a_cv(self, tmp_path):
        settings = _settings(tmp_path)
        first = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, first.id, CVDocument.model_validate(_VALID_CV))
        second = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, second.id, CVDocument.model_validate(_OTHER_CV))

        resolved = await cv_decks.resolve_path(settings, second.id)
        assert resolved == cv_decks.deck_path(settings, second.id)

    async def test_falls_back_to_default_when_requested_deck_has_no_cv(self, tmp_path, caplog):
        settings = _settings(tmp_path)
        default_deck = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, default_deck.id, CVDocument.model_validate(_VALID_CV))
        empty_deck = await cv_decks.create_deck(settings)  # never saved

        with caplog.at_level("WARNING"):
            resolved = await cv_decks.resolve_path(settings, empty_deck.id)
        assert resolved == cv_decks.deck_path(settings, default_deck.id)
        assert any("resolve_path" in r.message for r in caplog.records)

    async def test_none_deck_id_uses_default(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_VALID_CV))

        resolved = await cv_decks.resolve_path(settings, None)
        assert resolved == cv_decks.deck_path(settings, meta.id)

    async def test_no_decks_returns_none(self, tmp_path):
        settings = _settings(tmp_path)
        assert await cv_decks.resolve_path(settings, None) is None

    async def test_only_empty_deck_slot_resolves_to_none(self, tmp_path):
        """A has_cv=False slot must never be resolvable by the pipeline (Phase 2's
        cv_structure_exists = bool(resolve_path(settings, None)) relies on this)."""
        settings = _settings(tmp_path)
        await cv_decks.create_deck(settings)  # has_cv=False, no file on disk
        assert await cv_decks.resolve_path(settings, None) is None

    async def test_unknown_requested_id_falls_back_to_index_order(self, tmp_path):
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_VALID_CV))

        resolved = await cv_decks.resolve_path(settings, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        assert resolved == cv_decks.deck_path(settings, meta.id)

    async def test_malformed_requested_id_degrades_instead_of_raising(self, tmp_path):
        """base_cv_id will eventually come from the DB (Phase 3) -- a bad value must
        degrade to the fallback chain, never crash a dispatch."""
        settings = _settings(tmp_path)
        meta = await cv_decks.create_deck(settings)
        await cv_decks.save_deck(settings, meta.id, CVDocument.model_validate(_VALID_CV))

        resolved = await cv_decks.resolve_path(settings, "../x")
        assert resolved == cv_decks.deck_path(settings, meta.id)


# --- legacy migration -----------------------------------------------------------------------


class TestMigrateLegacy:
    async def test_migrates_legacy_file_into_one_default_deck(self, tmp_path):
        settings = _settings(tmp_path)
        cv = CVDocument.model_validate(_VALID_CV)
        await cv_structure.save(settings, cv)  # writes cv_structure.json only

        index = await cv_decks.load_index(settings)
        assert len(index.decks) == 1
        deck = index.decks[0]
        assert index.default_id == deck.id
        assert deck.has_cv is True
        assert deck.auto_title == "Jane Doe"

    async def test_migration_copies_file_and_leaves_legacy_in_place(self, tmp_path):
        settings = _settings(tmp_path)
        cv = CVDocument.model_validate(_VALID_CV)
        await cv_structure.save(settings, cv)
        legacy_bytes_before = settings.cv_structure_path.read_bytes()

        index = await cv_decks.load_index(settings)
        deck_id = index.decks[0].id

        assert settings.cv_structure_path.exists(), "legacy file must never be deleted"
        assert settings.cv_structure_path.read_bytes() == legacy_bytes_before, (
            "legacy file must never be rewritten"
        )
        assert cv_decks.deck_path(settings, deck_id).read_bytes() == legacy_bytes_before

    async def test_migration_is_a_noop_the_second_time(self, tmp_path):
        settings = _settings(tmp_path)
        await cv_structure.save(settings, CVDocument.model_validate(_VALID_CV))

        first_index = await cv_decks.load_index(settings)
        second_index = await cv_decks.load_index(settings)

        assert [m.id for m in first_index.decks] == [m.id for m in second_index.decks]
        assert first_index.default_id == second_index.default_id

    async def test_no_legacy_file_returns_empty_index_without_writing(self, tmp_path):
        settings = _settings(tmp_path)
        index = await cv_decks.load_index(settings)
        assert index == cv_decks.DeckIndex()
        assert not settings.cv_decks_path.exists()

    async def test_corrupt_legacy_file_treated_as_absent(self, tmp_path):
        settings = _settings(tmp_path)
        settings.cv_structure_path.parent.mkdir(parents=True, exist_ok=True)
        settings.cv_structure_path.write_text("not valid json", encoding="utf-8")

        index = await cv_decks.load_index(settings)
        assert index == cv_decks.DeckIndex()
        assert not settings.cv_decks_path.exists()
        # The corrupt legacy file itself is left alone.
        assert settings.cv_structure_path.read_text(encoding="utf-8") == "not valid json"

    async def test_concurrent_load_index_migrates_exactly_once(self, tmp_path):
        """First-boot race: GET /api/config and the orchestrator's dispatch gate can both
        call load_index() on the same event loop before either has written the index --
        both must see (and agree on) exactly one migrated deck, not one each."""
        settings = _settings(tmp_path)
        await cv_structure.save(settings, CVDocument.model_validate(_VALID_CV))

        results = await asyncio.gather(
            *(cv_decks.load_index(settings) for _ in range(20))
        )

        deck_ids = {index.decks[0].id for index in results}
        assert len(deck_ids) == 1, "every concurrent caller must see the same single deck"
        assert all(len(index.decks) == 1 for index in results)
        assert all(index.default_id == next(iter(deck_ids)) for index in results)

        final_index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert len(final_index.decks) == 1

        deck_files = list(settings.cv_decks_dir.glob("*.json"))
        assert len(deck_files) == 1


class TestIndexMutatorSerialization:
    """Every index mutator is a load-mutate-write cycle over one JSON file, and each of
    its steps awaits (`asyncio.to_thread`). Two overlapping mutations therefore interleave
    and the later write silently drops the earlier one unless they are serialized -- the
    reachable case being the editor rail's create/duplicate firing while a deck autosave
    PUT is still in flight."""

    async def test_concurrent_creates_all_survive(self, tmp_path):
        settings = _settings(tmp_path)

        metas = await asyncio.gather(
            *(cv_decks.create_deck(settings, name=f"deck-{i}") for i in range(10))
        )

        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert len(index.decks) == 10, "a lost update dropped one or more created decks"
        assert {m.id for m in metas} == {m.id for m in index.decks}
        assert index.default_id in {m.id for m in metas}

    async def test_concurrent_save_and_creates_keep_the_save(self, tmp_path):
        """The save's `has_cv=True`/`auto_title` refresh must not be clobbered by a
        create that loaded the index before the save wrote it -- a deck that loses it
        renders as an empty slot and PUT /api/jobs/{id}/base-cv 422s it despite the CV
        being on disk."""
        settings = _settings(tmp_path)
        target = await cv_decks.create_deck(settings, name="target")

        await asyncio.gather(
            cv_decks.save_deck(settings, target.id, CVDocument.model_validate(_VALID_CV)),
            *(cv_decks.create_deck(settings, name=f"other-{i}") for i in range(5)),
        )

        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert len(index.decks) == 6
        saved = next(m for m in index.decks if m.id == target.id)
        assert saved.has_cv is True
        assert saved.auto_title == "Jane Doe"


class TestEnsureDefaultDeck:
    """`ensure_default_deck` is the atomic replacement for the `load_index` -> `if
    default_id is None: create_deck` check-then-act that `PUT /api/cv-structure` used to
    inline. The read and the mint must share one hold of the index lock."""

    async def test_mints_the_first_deck_on_an_empty_index(self, tmp_path):
        settings = _settings(tmp_path)

        deck_id = await cv_decks.ensure_default_deck(settings)

        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert [m.id for m in index.decks] == [deck_id]
        assert index.default_id == deck_id
        assert index.decks[0].has_cv is False, "an ensured slot holds no CV until saved"

    async def test_returns_the_existing_default_without_minting(self, tmp_path):
        settings = _settings(tmp_path)
        existing = await cv_decks.create_deck(settings, name="mine")
        await cv_decks.create_deck(settings, name="other")

        assert await cv_decks.ensure_default_deck(settings) == existing.id

        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert len(index.decks) == 2, "ensure minted a deck despite a default existing"

    async def test_concurrent_first_installs_agree_on_one_deck(self, tmp_path):
        """The reason this function exists. Ten callers racing on a fresh install must all
        return the same id and leave exactly one deck -- the pre-fix inline shape had each
        one read an empty index, mint its own deck, and clobber the previous write."""
        settings = _settings(tmp_path)

        ids = await asyncio.gather(
            *(cv_decks.ensure_default_deck(settings) for _ in range(10))
        )

        assert len(set(ids)) == 1, f"callers disagreed on the default deck: {set(ids)}"
        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert [m.id for m in index.decks] == [ids[0]]
        assert index.default_id == ids[0]

    async def test_adopts_the_deck_a_legacy_migration_just_created(self, tmp_path):
        """On a pre-decks install `load_index` migrates `cv_structure.json` into one deck.
        Ensure must return *that* deck, not mint a second empty one beside the user's
        actual CV."""
        settings = _settings(tmp_path)
        await cv_structure.write(
            settings.cv_structure_path, CVDocument.model_validate(_VALID_CV)
        )

        deck_id = await cv_decks.ensure_default_deck(settings)

        index = await cv_decks.read_index(cv_decks.index_path(settings))
        assert [m.id for m in index.decks] == [deck_id]
        assert index.decks[0].has_cv is True
        assert index.decks[0].auto_title == "Jane Doe"
