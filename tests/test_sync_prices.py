"""Tests for `scripts/sync_prices.py`, the builder of the committed snapshot.

Run on miniature upstream files rather than the network: what is pinned is the
transformation -- per-token to per-MTok, which keys survive, which catalog
wins, how history is kept -- because a slip there misprices every model
silently and the snapshot diff is too big to catch it by eye.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "sync_prices", Path(__file__).resolve().parents[1] / "scripts" / "sync_prices.py")
sp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sp)


def ll(inp, out, *, provider="anthropic", mode="chat", **extra):
    return {"litellm_provider": provider, "mode": mode,
            "input_cost_per_token": inp, "output_cost_per_token": out, **extra}


LITELLM = {
    "sample_spec": {"litellm_provider": "one of ...", "input_cost_per_token": 0.0},
    "claude-opus-5-5": ll(4e-06, 2e-05, cache_read_input_token_cost=2e-07,
                          cache_creation_input_token_cost=5e-06,
                          cache_creation_input_token_cost_above_1hr=8e-06),
    "claude-haiku-9": ll(1e-06, 5e-06),  # no published 1h rate: Anthropic's 2x rule
    "gpt-6.1-sol": ll(2e-06, 1e-05, provider="openai", cache_read_input_token_cost=1e-07,
                      input_cost_per_token_above_272k_tokens=4e-06),
    "gpt-image-9": ll(5e-06, 4e-05, provider="openai", mode="image_generation"),
    "azure/gpt-6.1-sol": ll(9e-06, 9e-05, provider="azure"),
    "openrouter/openai/gpt-6.1-sol": ll(9e-06, 9e-05, provider="openrouter"),
    "xai/grok-9": ll(2e-06, 6e-06, provider="xai"),
    "anthropic.claude-3-old-20240101-v1:0": ll(3e-06, 1.5e-05, provider="bedrock",
                                               cache_read_input_token_cost=3e-07),
}

MODELS_DEV = {
    "opencode": {"id": "opencode", "models": {
        "grok-9": {"cost": {"input": 9, "output": 9}},
        "muse-spark-9": {"cost": {"input": 1.5, "output": 5}},
        "muse-spark-9-contributor-free": {"cost": {"input": 0, "output": 0}},
        "zen-only-9": {"cost": {"input": 1, "output": 2}},
    }},
    "meta": {"id": "meta", "models": {
        "muse-spark-9": {"cost": {"input": 1.25, "output": 4.25, "cache_read": 0.15}},
    }},
    "anthropic": {"id": "anthropic", "models": {}},
    "openai": {"id": "openai", "models": {}},
    # A reseller's own claude id, which no vendor or platform lists.
    "venice": {"id": "venice", "models": {
        "claude-opus-5-5-turbo": {"cost": {"input": 9.6, "output": 48}},
    }},
}


@pytest.fixture
def built():
    return sp.build(LITELLM, MODELS_DEV, {"repo": "BerriAI/litellm"})["models"]


def current(entry):
    return sp.rates(entry["clauses"][-1])


def test_litellm_rates_become_per_million_without_float_noise(built):
    assert current(built["claude-opus-5-5"]) == (4.0, 20.0, 0.2, 5.0, 8.0)


def test_the_published_one_hour_rate_is_kept_and_only_anthropic_gets_the_2x_rule(built):
    assert current(built["claude-haiku-9"])[4] == 2.0
    assert current(built["gpt-6.1-sol"])[4] is None


def test_a_long_context_tier_is_flattened_and_marked(built):
    assert built["gpt-6.1-sol"]["clauses"][-1]["tiered"] is True
    assert current(built["gpt-6.1-sol"])[:2] == (2.0, 10.0)


def test_only_first_party_text_keys_are_kept(built):
    assert "gpt-image-9" not in built
    assert "azure/gpt-6.1-sol" not in built
    assert "openrouter/openai/gpt-6.1-sol" not in built
    assert "sample_spec" not in built


def test_a_zen_model_litellm_keys_under_its_vendor_is_priced_from_litellm(built):
    entry = built["grok-9"]
    assert entry["source"] == "litellm" and entry["upstream"] == "xai/grok-9"
    assert current(entry)[:2] == (2.0, 6.0)


def test_models_dev_fills_in_only_what_litellm_lacks_and_the_owner_wins(built):
    entry = built["muse-spark-9"]
    assert entry["source"] == "models.dev"
    assert entry["upstream"] == "meta/muse-spark-9" and entry["trust"] == "owner"
    assert current(entry)[:3] == (1.25, 4.25, 0.15)
    # A Zen-only id is what an opencode user pays, gateway or not.
    assert built["zen-only-9"]["trust"] == "reseller"


def test_free_tiers_and_reseller_inventions_are_left_out(built):
    assert not any(sp.is_free_tier(k) for k in built)
    assert "claude-opus-5-5-turbo" not in built


def test_a_retired_claude_model_is_priced_from_bedrocks_key(built):
    entry = built["claude-3-old"]
    assert entry["upstream"] == "anthropic.claude-3-old-20240101-v1:0"
    assert current(entry) == (3.0, 15.0, 0.3, None, 6.0)


def test_a_changed_rate_keeps_its_history_and_is_reported(built):
    previous = {"models": {"claude-opus-5-5": {
        "source": "litellm", "upstream": "claude-opus-5-5",
        "clauses": [{"start_date": None, "input_mtok": 5.0, "output_mtok": 25.0,
                     "cache_read_mtok": 0.5, "cache_write_mtok": 6.25,
                     "cache_write_1h_mtok": 10.0}]}}}
    snapshot = {"models": built}
    sp.carry_history(snapshot, previous, "2026-10-06")
    clauses = built["claude-opus-5-5"]["clauses"]
    assert [c["start_date"] for c in clauses] == [None, "2026-10-06"]
    assert sp.rates(clauses[0])[0] == 5.0 and sp.rates(clauses[1])[0] == 4.0
    assert [k for k, _, _ in sp.changes(previous, snapshot)] == ["claude-opus-5-5"]


def test_a_rebased_key_drops_its_history(built):
    previous = {"models": {"claude-opus-5-5": {
        "clauses": [{"start_date": None, "input_mtok": 5.0, "output_mtok": 25.0}]}}}
    sp.carry_history({"models": built}, previous, "2026-10-06", rebase=["claude-opus-5-5"])
    assert [c["start_date"] for c in built["claude-opus-5-5"]["clauses"]] == [None]


def test_an_unchanged_rate_keeps_its_original_date(built):
    same = dict(built["claude-opus-5-5"]["clauses"][0], start_date="2026-01-01")
    previous = {"models": {"claude-opus-5-5": {"clauses": [same]}}}
    sp.carry_history({"models": built}, previous, "2026-10-06")
    assert [c["start_date"] for c in built["claude-opus-5-5"]["clauses"]] == ["2026-01-01"]
