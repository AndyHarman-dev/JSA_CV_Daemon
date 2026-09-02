"""Offline tests for `jsa/agents/model_costs.py` -- cost data + ordering
primitives for the (not-yet-wired) model ladder. No network access; the
OpenRouter live-pricing tests exercise `record_openrouter_pricing` directly
rather than going through `model_catalog._fetch_openrouter`'s httpx call."""

from __future__ import annotations

import pytest

import jsa.agents.model_costs as model_costs
from jsa.agents.model_catalog import DEFAULT_CATALOG
from jsa.agents.model_costs import (
    STATIC_COSTS,
    cost_for,
    cost_ordered,
    next_model,
    record_openrouter_pricing,
)
from jsa.agents.opencode_go import _PROTOCOL as OPENCODE_GO_PROTOCOL


@pytest.fixture(autouse=True)
def _clear_openrouter_pricing_cache():
    """The live-pricing cache is process-global -- isolate every test."""
    model_costs._openrouter_live_costs.clear()
    yield
    model_costs._openrouter_live_costs.clear()


class TestCostOrdered:
    def test_opencode_go_ascending_order(self):
        models = list(DEFAULT_CATALOG["opencode-go"])
        ordered = cost_ordered("opencode-go", models)

        costs = [cost_for("opencode-go", m) for m in ordered]
        assert costs == sorted(costs)
        assert ordered[0] == "mimo-v2.5"  # cheapest: $0.28/1M
        assert ordered[-1] == "kimi-k3"  # priciest: $15.00/1M

    def test_opencode_zen_all_free_is_a_stable_no_op(self):
        """Every opencode-zen model is $0.00 -- cost_ordered must not reorder
        the catalog at all (a stable sort over an all-tied key is a no-op)."""
        models = list(DEFAULT_CATALOG["opencode-zen"])
        assert cost_ordered("opencode-zen", models) == models

    def test_unknown_cost_models_sort_last(self):
        models = ["kimi-k3", "totally-unknown-model", "mimo-v2.5"]
        ordered = cost_ordered("opencode-go", models)
        assert ordered == ["mimo-v2.5", "kimi-k3", "totally-unknown-model"]

    def test_multiple_unknown_cost_models_stay_in_relative_order(self):
        """Two unknown-cost models must keep their INPUT relative order
        (stability), not be reordered alphabetically or otherwise."""
        models = ["unknown-b", "mimo-v2.5", "unknown-a"]
        ordered = cost_ordered("opencode-go", models)
        assert ordered == ["mimo-v2.5", "unknown-b", "unknown-a"]

    def test_equal_cost_models_keep_catalog_order(self):
        """glm-5.3, glm-5.2, glm-5.1 all cost $4.40 -- a tie must not
        reorder them relative to each other."""
        models = ["glm-5.1", "glm-5.3", "glm-5.2"]
        assert cost_ordered("opencode-go", models) == ["glm-5.1", "glm-5.3", "glm-5.2"]


class TestNextModel:
    def test_returns_next_rung(self):
        assert next_model("opencode-go", "mimo-v2.5", ["mimo-v2.5", "qwen3.8-flash"]) == (
            "qwen3.8-flash"
        )

    def test_returns_none_at_top_rung(self):
        models = list(DEFAULT_CATALOG["opencode-go"])
        assert next_model("opencode-go", "kimi-k3", models) is None

    def test_returns_none_when_current_not_in_models(self):
        assert next_model("opencode-go", "not-in-list", ["mimo-v2.5", "kimi-k3"]) is None

    def test_hops_correctly_through_a_tie(self):
        """glm-5.3-flash ($0.50) -> next distinct-or-tied rung should be
        hy3 ($0.58), the next item in cost order, not skipped."""
        models = ["glm-5.3-flash", "hy3", "mimo-v2.5"]
        # cost order: mimo-v2.5 (0.28) < glm-5.3-flash (0.50) < hy3 (0.58)
        assert next_model("opencode-go", "glm-5.3-flash", models) == "hy3"
        assert next_model("opencode-go", "mimo-v2.5", models) == "glm-5.3-flash"


class TestCostFor:
    def test_unknown_backend_returns_none(self):
        assert cost_for("not-a-real-backend", "whatever") is None

    def test_unknown_model_on_known_backend_returns_none(self):
        assert cost_for("opencode-go", "not-a-real-model") is None

    def test_google_cli_has_no_cost_table(self):
        """google-cli has no model concept at all -- see module docstring."""
        assert "google-cli" not in STATIC_COSTS


class TestOpenRouterLivePricing:
    def test_live_pricing_overrides_static_entry(self, monkeypatch):
        monkeypatch.setitem(STATIC_COSTS["openrouter"], "some/model", 5.0)
        record_openrouter_pricing({"some/model": 1.23})

        assert cost_for("openrouter", "some/model") == 1.23

    def test_no_live_entry_falls_back_to_static(self, monkeypatch):
        monkeypatch.setitem(STATIC_COSTS["openrouter"], "some/other-model", 5.0)
        record_openrouter_pricing({"unrelated/model": 1.23})

        assert cost_for("openrouter", "some/other-model") == 5.0

    def test_record_replaces_not_merges(self):
        record_openrouter_pricing({"a/model": 1.0, "b/model": 2.0})
        record_openrouter_pricing({"a/model": 9.0})

        assert cost_for("openrouter", "a/model") == 9.0
        assert cost_for("openrouter", "b/model") is None


class TestOpencodeGoCoverageGuard:
    def test_every_protocol_model_has_a_static_cost(self):
        """The most important test in this file: if a new `_PROTOCOL` entry
        (jsa/agents/opencode_go.py) is added without a matching STATIC_COSTS
        price, every model ties on cost and a stable sort silently degrades
        the ladder to catalog (effectively alphabetical) order -- defeating
        the entire feature on the one backend it was built for."""
        missing = sorted(set(OPENCODE_GO_PROTOCOL) - set(STATIC_COSTS["opencode-go"]))
        assert not missing, f"opencode-go models missing a STATIC_COSTS entry: {missing}"

    def test_no_stale_cost_entries_for_removed_protocol_models(self):
        extra = sorted(set(STATIC_COSTS["opencode-go"]) - set(OPENCODE_GO_PROTOCOL))
        assert not extra, f"STATIC_COSTS has entries for unknown opencode-go models: {extra}"
