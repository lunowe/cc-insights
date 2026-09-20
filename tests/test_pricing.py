"""Tests for the price table.

The load-bearing ones are `test_a_rate_that_starts_later_does_not_price_an
_earlier_event` and `test_sync_never_overwrites_a_manual_rate`: the first is
what stops a vendor's price change from silently rewriting last month, the
second is the only escape hatch a user has when the catalog is wrong about
their model.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from cc_insights import db, pricing

# A catalog small enough to read, exercising every matcher form and a dated
# price change. Shaped exactly like scripts/sync_prices.py writes one.
CATALOG = {
    "source": {"repo": "test/catalog", "commit": "abc123", "fetched_at": "2026-01-01T00:00:00Z"},
    "provider_order": ["anthropic", "openai", "openrouter"],
    "models": [
        {"provider": "anthropic", "id": "claude-test",
         "match": {"starts_with": "claude-test"},
         "clauses": [
             {"start_date": None, "input_mtok": 3.0, "output_mtok": 15.0,
              "cache_read_mtok": 0.3, "cache_write_mtok": 3.75},
             {"start_date": "2026-06-01", "input_mtok": 5.0, "output_mtok": 25.0,
              "cache_read_mtok": 0.5, "cache_write_mtok": 6.25},
         ]},
        {"provider": "openai", "id": "gpt-test",
         "match": {"or": [{"equals": "gpt-test"}, {"regex": r"^gpt-test-\d+$"}]},
         "clauses": [{"start_date": None, "input_mtok": 1.0, "output_mtok": 4.0,
                      "cache_read_mtok": 0.1, "cache_write_mtok": None}]},
        # Same string matches here too; the earlier provider must win.
        {"provider": "openrouter", "id": "gpt-test-reseller",
         "match": {"contains": "gpt-test"},
         "clauses": [{"start_date": None, "input_mtok": 99.0, "output_mtok": 99.0,
                      "cache_read_mtok": 99.0, "cache_write_mtok": 99.0}]},
    ],
}

JUNE = pricing.day_to_ms("2026-06-01")
MAY = JUNE - 86_400_000


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch):
    path = tmp_path / "model_prices.json"
    path.write_text(json.dumps(CATALOG))
    monkeypatch.setattr(pricing, "CATALOG_PATH", path)
    pricing.catalog.cache_clear()
    yield path
    pricing.catalog.cache_clear()


def add_event(conn: sqlite3.Connection, model: str | None, ts: int = MAY) -> None:
    """Enough of an event for `models_in_use` to see it."""
    conn.execute("INSERT OR IGNORE INTO host (host_id, hostname, first_seen, last_seen)"
                 " VALUES ('h', 'H', 0, 0)")
    conn.execute("INSERT OR IGNORE INTO session (id, native_id, source, host_id,"
                 " started_at, ended_at) VALUES ('s', 'n', 'claude_code', 'h', 0, 0)")
    conn.execute("INSERT OR IGNORE INTO thread (id, native_id, session_id, started_at,"
                 " ended_at) VALUES ('t', 'n', 's', 0, 0)")
    n = conn.execute("SELECT count(*) FROM event").fetchone()[0]
    conn.execute("INSERT INTO event (id, session_id, thread_id, native_event_id, ts, kind,"
                 " model) VALUES (?, 's', 't', ?, ?, 'assistant', ?)",
                 (f"e{n}", f"n{n}", ts, model))


# --- dates --------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    (None, 0), ("", 0), ("not-a-date", 0), ("1970-01-01", 0),
    ("2026-06-01", 1_780_272_000_000),
])
def test_day_to_ms(raw, expected):
    assert pricing.day_to_ms(raw) == expected


def test_zero_means_since_records_began_not_1970(catalog):
    """A clause with no start date must price the oldest event in the corpus."""
    rates = pricing.rates_from_catalog("claude-test")
    assert rates[0].effective_from == 0
    assert pricing.rates_at(rates, ts=1) is rates[0]


# --- matching -----------------------------------------------------------


@pytest.mark.parametrize("model, provider", [
    ("claude-test", "anthropic"),
    ("claude-test-20260101", "anthropic"),
    ("gpt-test", "openai"),
    ("gpt-test-5", "openai"),
])
def test_resolve_matches_every_form(catalog, model, provider):
    assert pricing.resolve(model)["provider"] == provider


def test_the_first_provider_in_order_wins(catalog):
    """A bare model string can match a reseller too; first-party must win, or
    a $99 openrouter row prices traffic that went to OpenAI."""
    assert pricing.resolve("gpt-test")["id"] == "gpt-test"


def test_an_unknown_model_resolves_to_nothing(catalog):
    assert pricing.resolve("llama-9") is None
    assert pricing.rates_from_catalog("llama-9") == []


def test_a_missing_catalog_is_not_an_error(tmp_path, monkeypatch):
    """Every model unpriced beats an exception out of `cci stats`."""
    monkeypatch.setattr(pricing, "CATALOG_PATH", tmp_path / "gone.json")
    pricing.catalog.cache_clear()
    try:
        assert pricing.resolve("claude-test") is None
        assert pricing.catalog_source() == {}
    finally:
        pricing.catalog.cache_clear()


# --- dated rates --------------------------------------------------------


def test_rates_at_takes_the_newest_rate_not_later_than_the_event(catalog):
    rates = pricing.rates_from_catalog("claude-test")
    assert pricing.rates_at(rates, MAY).input_mtok == 3.0
    assert pricing.rates_at(rates, JUNE).input_mtok == 5.0
    assert pricing.rates_at(rates, JUNE + 1).input_mtok == 5.0


def test_a_rate_that_starts_later_does_not_price_an_earlier_event(catalog):
    """The whole reason prices are dated: today's rate must not rewrite
    last month's total."""
    later_only = [pricing.Rates(effective_from=JUNE, input_mtok=5.0)]
    assert pricing.rates_at(later_only, MAY) is None


# --- sync ---------------------------------------------------------------


def test_sync_writes_every_dated_rate_for_models_in_use(conn, catalog):
    add_event(conn, "claude-test")
    add_event(conn, "gpt-test")
    r = pricing.sync(conn)
    assert sorted(r.priced) == ["claude-test", "gpt-test"]
    assert r.unpriced == [] and r.rows_written == 3  # two dated + one
    rows = conn.execute("SELECT model, effective_from FROM model_price ORDER BY 1, 2").fetchall()
    assert [tuple(x) for x in rows] == [
        ("claude-test", 0), ("claude-test", JUNE), ("gpt-test", 0)]


def test_sync_records_which_catalog_entry_priced_a_model(conn, catalog):
    add_event(conn, "claude-test-20260101")
    pricing.sync(conn)
    row = conn.execute("SELECT matched_id, origin FROM model_price").fetchone()
    assert row["matched_id"] == "anthropic/claude-test" and row["origin"] == "genai-prices"


def test_sync_is_idempotent(conn, catalog):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    before = conn.execute("SELECT count(*) FROM model_price").fetchone()[0]
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == before


def test_sync_reports_a_model_it_cannot_price(conn, catalog):
    add_event(conn, "llama-9")
    r = pricing.sync(conn)
    assert r.unpriced == ["llama-9"] and r.priced == []


def test_sync_drops_a_catalog_rate_that_upstream_withdrew(conn, catalog, tmp_path, monkeypatch):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 2

    shrunk = dict(CATALOG)
    shrunk["models"] = [{**CATALOG["models"][0], "clauses": CATALOG["models"][0]["clauses"][:1]}]
    (tmp_path / "model_prices.json").write_text(json.dumps(shrunk))
    pricing.catalog.cache_clear()
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 1


def test_sync_never_overwrites_a_manual_rate(conn, catalog):
    """A human beat the catalog, the same rule project_group.origin follows."""
    add_event(conn, "claude-test")
    pricing.set_price(conn, "claude-test", input_mtok=1.23, output_mtok=4.56)
    r = pricing.sync(conn)

    row = conn.execute(
        "SELECT input_mtok, origin FROM model_price WHERE model = 'claude-test'"
        " AND effective_from = 0").fetchone()
    assert row["input_mtok"] == 1.23 and row["origin"] == "manual"
    assert r.rows_kept_manual == 1
    # The catalog's *other* dated rate is still written alongside it.
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 2


def test_a_manual_rate_can_be_cleared_back_to_the_catalog(conn, catalog):
    add_event(conn, "claude-test")
    pricing.set_price(conn, "claude-test", input_mtok=1.23)
    assert pricing.clear_price(conn, "claude-test") == 1
    pricing.sync(conn)
    row = conn.execute(
        "SELECT input_mtok, origin FROM model_price WHERE model = 'claude-test'"
        " AND effective_from = 0").fetchone()
    assert row["input_mtok"] == 3.0 and row["origin"] == "genai-prices"


def test_clear_leaves_catalog_rows_alone(conn, catalog):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    assert pricing.clear_price(conn, "claude-test") == 0
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 2


def test_manual_and_catalog_cannot_collide_on_one_date(conn, catalog):
    """The primary key enforces one rate per (model, date); a second write at
    the same date replaces rather than duplicating."""
    pricing.set_price(conn, "m", input_mtok=1.0)
    pricing.set_price(conn, "m", input_mtok=2.0)
    rows = conn.execute("SELECT input_mtok FROM model_price WHERE model = 'm'").fetchall()
    assert [r[0] for r in rows] == [2.0]


# --- what a total must disclose -----------------------------------------


def test_approximations_names_a_model_priced_as_a_neighbour(conn, catalog):
    """`claude-test-20260101` priced as `claude-test` is defensible; invisible
    it is not. This is the list every surface prints beside its total."""
    add_event(conn, "claude-test-20260101")
    add_event(conn, "claude-test")
    pricing.sync(conn)
    assert pricing.approximations(conn) == [
        ("claude-test-20260101", "anthropic/claude-test")]


def test_a_manual_rate_is_never_reported_as_an_approximation(conn, catalog):
    pricing.set_price(conn, "whatever", input_mtok=1.0)
    assert pricing.approximations(conn) == []


def test_models_in_use_reads_every_model_any_event_names(conn, catalog):
    add_event(conn, "claude-test")
    add_event(conn, "gpt-test")
    add_event(conn, None)
    assert pricing.models_in_use(conn) == ["claude-test", "gpt-test"]


def test_load_rates_groups_by_model_oldest_first(conn, catalog):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    loaded = pricing.load_rates(conn)
    assert [r.effective_from for r in loaded["claude-test"]] == [0, JUNE]


def test_the_shipped_catalog_is_readable_and_dated():
    """The committed snapshot itself, not a fixture: a truncated or
    hand-edited file would make every model silently unpriced."""
    doc = json.loads(pricing.CATALOG_PATH.read_text())
    assert doc["source"]["repo"] == "pydantic/genai-prices"
    assert len(doc["models"]) > 100
    assert all(m["clauses"] and m["match"] for m in doc["models"])
