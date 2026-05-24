"""Tests for jsa/agents/registry.py: register() and backend_for()."""

from __future__ import annotations

import pytest

from jsa.agents.registry import _REGISTRY, backend_for, register
from tests.backend.fakes.fake_backend import FakeAgentBackend


# ---------------------------------------------------------------------------
# Parameterless subclass — needed because backend_for() calls cls() with no args
# and FakeAgentBackend requires a `replies` argument.
# ---------------------------------------------------------------------------

class _NoArgFake(FakeAgentBackend):
    """FakeAgentBackend with a no-arg constructor, suitable for registry tests."""

    def __init__(self) -> None:
        super().__init__([])


# ---------------------------------------------------------------------------
# Fixture: save/restore the registry around each test so tests are isolated.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    """Save and restore _REGISTRY before and after every test in this module."""
    saved = dict(_REGISTRY)
    yield _REGISTRY
    _REGISTRY.clear()
    _REGISTRY.update(saved)


# ---------------------------------------------------------------------------
# 1 — register()
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_stores_class_under_name(self):
        register("test-fake", _NoArgFake)
        assert _REGISTRY["test-fake"] is _NoArgFake

    def test_register_overwrites_existing_entry(self):
        """Calling register() twice with the same name replaces the entry."""
        class _AltFake(_NoArgFake):
            pass

        register("test-fake", _NoArgFake)
        register("test-fake", _AltFake)
        assert _REGISTRY["test-fake"] is _AltFake

    def test_register_multiple_distinct_names(self):
        class _FakeA(_NoArgFake):
            pass

        class _FakeB(_NoArgFake):
            pass

        register("test-fake-a", _FakeA)
        register("test-fake-b", _FakeB)
        assert _REGISTRY["test-fake-a"] is _FakeA
        assert _REGISTRY["test-fake-b"] is _FakeB


# ---------------------------------------------------------------------------
# 2 — backend_for() happy path
# ---------------------------------------------------------------------------

class TestBackendFor:
    def test_backend_for_returns_instance_of_registered_class(self):
        register("test-fake", _NoArgFake)
        instance = backend_for("test-fake")
        assert isinstance(instance, _NoArgFake)

    def test_backend_for_is_also_instance_of_fake_agent_backend(self):
        """The returned object is a FakeAgentBackend since _NoArgFake subclasses it."""
        register("test-fake", _NoArgFake)
        instance = backend_for("test-fake")
        assert isinstance(instance, FakeAgentBackend)

    def test_backend_for_creates_new_instance_each_call(self):
        """Each call to backend_for() must produce a distinct object."""
        register("test-fake", _NoArgFake)
        instance_a = backend_for("test-fake")
        instance_b = backend_for("test-fake")
        assert instance_a is not instance_b


# ---------------------------------------------------------------------------
# 3 — backend_for() with an unknown name
# ---------------------------------------------------------------------------

class TestBackendForUnknown:
    def test_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError):
            backend_for("no-such-backend")

    def test_error_message_mentions_unknown_name(self):
        with pytest.raises(KeyError) as exc_info:
            backend_for("no-such-backend")
        assert "no-such-backend" in str(exc_info.value)

    def test_error_message_lists_registered_backends(self):
        """The error message should include the names of available backends."""
        register("test-fake", _NoArgFake)
        with pytest.raises(KeyError) as exc_info:
            backend_for("no-such-backend")
        assert "test-fake" in str(exc_info.value)

    def test_error_message_none_registered_when_registry_empty(self):
        """When no backends are registered the message says '(none registered)'."""
        # Fixture ensures _REGISTRY is restored; clear it explicitly here.
        _REGISTRY.clear()
        with pytest.raises(KeyError) as exc_info:
            backend_for("anything")
        assert "(none registered)" in str(exc_info.value)

    def test_error_message_lists_all_registered_names(self):
        """All currently registered names should appear in the error message."""
        class _FakeX(_NoArgFake):
            pass

        class _FakeY(_NoArgFake):
            pass

        register("backend-x", _FakeX)
        register("backend-y", _FakeY)

        with pytest.raises(KeyError) as exc_info:
            backend_for("missing-backend")

        msg = str(exc_info.value)
        assert "backend-x" in msg
        assert "backend-y" in msg
