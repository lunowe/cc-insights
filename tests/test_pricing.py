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
        # Provenance still reports the shipped corrections, which are a
        # separate file and unaffected by a missing catalog.
        assert pricing.catalog_source().keys() == {"overrides"}
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


# --- the shipped corrections -------------------------------------------
#
# A rate correction that lives in one laptop's database is lost on the next
# machine and on the next rebuild. `price_overrides.json` is the layer that
# makes one durable, and these tests pin the two properties that make it safe:
# it beats the catalog, and a human still beats it.
#
# Several of these deliberately do NOT take the `catalog` fixture: they are
# assertions about the files this repo ships, and running them against the
# miniature test catalog would prove nothing about what a user gets.

FABLE_51 = "claude-fable-5-1"
FABLE_5 = "claude-fable-5"


def test_the_shipped_corrections_parse_and_carry_their_provenance():
    doc = json.loads(pricing.OVERRIDES_PATH.read_text())
    assert doc["models"], "no corrections shipped"
    for entry in doc["models"]:
        assert entry["match"] and entry["clauses"]
        # Every one of these is a claim about someone else's price list. It
        # is only defensible with a source and a date beside it.
        assert entry["source"].startswith("https://")
        assert entry["checked"] and entry["why"]


def test_fable_5_1_and_fable_5_are_priced_differently():
    """The whole reason this layer exists. They differ ONLY in cache reads,
    and on a cache-read-dominated corpus that is a fifth of the total."""
    five = {r.cache_read_mtok for r in pricing.rates_from_catalog(FABLE_5)}
    five_one = {r.cache_read_mtok for r in pricing.rates_from_catalog(FABLE_51)}
    assert five_one == {0.25}
    assert five == {1.0}

    # ...and nothing else about them differs, which is what makes a
    # too-broad matcher so easy to write and so quiet when it is wrong.
    for field in ("input_mtok", "output_mtok", "cache_write_mtok"):
        a = {getattr(r, field) for r in pricing.rates_from_catalog(FABLE_5)}
        b = {getattr(r, field) for r in pricing.rates_from_catalog(FABLE_51)}
        assert a == b, field


def test_no_correction_matches_a_model_it_does_not_name():
    """A matcher broader than its own model reintroduces the bug it fixes."""
    doc = json.loads(pricing.OVERRIDES_PATH.read_text())
    ids = [e["id"] for e in doc["models"]]
    for entry in doc["models"]:
        for other in ids:
            if other == entry["id"] or other.startswith(entry["id"]):
                continue
            assert not pricing._matches(entry["match"], other), (
                f"{entry['id']}'s matcher also catches {other}")


def test_a_correction_beats_the_catalog(conn):
    add_event(conn, FABLE_51)
    pricing.sync(conn)
    row = conn.execute(
        "SELECT cache_read_mtok, origin, note FROM model_price WHERE model = ?",
        (FABLE_51,)).fetchone()
    assert row["cache_read_mtok"] == 0.25
    assert row["origin"] == pricing.OVERRIDE
    assert row["note"], "a correction must carry its reason into the table"


def test_a_human_still_beats_a_correction(conn):
    add_event(conn, FABLE_51)
    pricing.set_price(conn, FABLE_51, cache_read_mtok=0.11)
    pricing.sync(conn)
    row = conn.execute(
        "SELECT cache_read_mtok, origin FROM model_price WHERE model = ?",
        (FABLE_51,)).fetchone()
    assert row["cache_read_mtok"] == 0.11 and row["origin"] == pricing.MANUAL


def test_a_corrected_model_is_not_reported_as_an_approximation(conn):
    """It is no longer priced as a relative -- it has a rate of its own."""
    add_event(conn, FABLE_51)
    pricing.sync(conn)
    assert pricing.approximations(conn) == []


def test_sync_reports_which_models_a_correction_priced(conn, catalog):
    add_event(conn, FABLE_51)
    add_event(conn, "claude-test")
    r = pricing.sync(conn)
    assert r.overridden == [FABLE_51]
    assert set(r.priced) == {FABLE_51, "claude-test"}


def test_sync_flags_a_correction_upstream_has_caught_up_with(conn, catalog, monkeypatch):
    """An override the catalog now agrees with is not a correction any more,
    only a way to go stale."""
    caught_up = dict(CATALOG)
    caught_up["models"] = CATALOG["models"] + [{
        "provider": "anthropic", "id": FABLE_51,
        "match": {"equals": FABLE_51},
        "clauses": [{"start_date": None, "input_mtok": 10.0, "output_mtok": 50.0,
                     "cache_read_mtok": 0.25, "cache_write_mtok": 12.5}],
    }]
    (catalog).write_text(json.dumps(caught_up))
    pricing.catalog.cache_clear()

    add_event(conn, FABLE_51)
    r = pricing.sync(conn)
    assert r.redundant == [FABLE_51]
    # Still applied -- flagging is not the same as silently standing down.
    assert r.overridden == [FABLE_51]


def test_the_corrections_match_the_vendors_published_rates():
    """These four numbers were read off the vendor's pricing page on the date
    each entry records. If one changes, the entry is stale, not the test."""
    expected = {
        "claude-fable-5-1": (10, 50, 0.25, 12.5),
        "claude-mythos-5-1": (10, 50, 0.25, 12.5),
        "claude-mythos-5": (10, 50, 1, 12.5),
        "claude-sonnet-5": (2, 10, 0.2, 2.5),
    }
    doc = json.loads(pricing.OVERRIDES_PATH.read_text())
    got = {
        e["id"]: (c["input_mtok"], c["output_mtok"],
                  c["cache_read_mtok"], c["cache_write_mtok"])
        for e in doc["models"] for c in e["clauses"]
    }
    assert got == expected
