"""Tests for the price table.

The load-bearing ones are `test_a_rate_that_starts_later_does_not_price_an
_earlier_event` and `test_sync_never_overwrites_a_manual_rate`: the first is
what stops a vendor's price change from silently rewriting last month, the
second is the only escape hatch a user has when the catalog is wrong about
their model. The matching tests pin the rule that made this catalog switch
necessary: `claude-opus-5-5` must never be priced as `claude-opus-5`.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from cc_insights import pricing


def clause(i, o, cr=None, cw=None, cw1h=None, start=None):
    return {"start_date": start, "input_mtok": i, "output_mtok": o, "cache_read_mtok": cr,
            "cache_write_mtok": cw, "cache_write_1h_mtok": cw1h}


def litellm(*clauses, upstream=None, provider="anthropic"):
    entry = {"source": "litellm", "provider": provider, "clauses": list(clauses)}
    if upstream:
        entry["upstream"] = upstream
    return entry


# A catalog small enough to read, shaped exactly like scripts/sync_prices.py
# writes one: version siblings that differ in price, a dated price change, a
# prefixed key, and a models.dev fallback entry.
CATALOG = {
    "source": {"repo": "BerriAI/litellm", "commit": "abc123",
               "fetched_at": "2026-01-01T00:00:00Z"},
    "models": {
        "claude-test": litellm(clause(3.0, 15.0, 0.3, 3.75, 6.0),
                               clause(5.0, 25.0, 0.5, 6.25, 10.0, start="2026-06-01")),
        "claude-opus-5": litellm(clause(5.0, 25.0, 0.5, 6.25, 10.0)),
        "claude-opus-5-5": litellm(clause(4.0, 20.0, 0.2, 5.0, 8.0)),
        "claude-fable-5": litellm(clause(10.0, 50.0, 1.0, 12.5, 20.0)),
        "gpt-6-sol": litellm(clause(2.0, 10.0, 0.2, 2.5), provider="openai"),
        "gpt-5": litellm(clause(1.25, 10.0, 0.125), provider="openai"),
        "gpt-5.3-codex": litellm(clause(1.75, 14.0, 0.175), provider="openai"),
        "o1": litellm(clause(15.0, 60.0, 7.5), provider="openai"),
        "openai/gpt-prefixed": litellm(clause(7.0, 7.0), provider="openai"),
        "muse-spark-1.3": {"source": "models.dev", "upstream": "meta/muse-spark-1.3",
                           "provider": "meta", "trust": "owner",
                           "clauses": [clause(1.25, 4.25, 0.15)]},
    },
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


@pytest.fixture
def overrides(tmp_path: Path, monkeypatch):
    """A corrections file of the test's own; the shipped one may be empty."""
    path = tmp_path / "price_overrides.json"

    def write(*entries):
        path.write_text(json.dumps({"why": "test", "models": list(entries)}))
        pricing.override_catalog.cache_clear()

    write()
    monkeypatch.setattr(pricing, "OVERRIDES_PATH", path)
    yield write
    pricing.override_catalog.cache_clear()


FABLE_51_FIX = {
    "provider": "anthropic", "id": "claude-fable-5-1", "why": "cache reads are 0.025x",
    "source": "https://example.test/pricing", "checked": "2026-09-20",
    "clauses": [clause(10, 50, 0.25, 12.5, 20)],
}


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


def key_for(model):
    hit = pricing.resolve(model)
    return None if hit is None else (hit["key"], hit["exact"])


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


@pytest.mark.parametrize("model, key", [
    ("claude-opus-5-5", "claude-opus-5-5"),              # the key itself
    ("CLAUDE-OPUS-5-5", "claude-opus-5-5"),              # as some tool spells it
    ("anthropic/claude-opus-5-5", "claude-opus-5-5"),    # provider prefix
    ("openrouter/anthropic/claude-opus-5", "claude-opus-5"),
    ("claude-opus-5-5-20260922", "claude-opus-5-5"),     # Anthropic date suffix
    ("gpt-5-2025-08-07", "gpt-5"),                       # OpenAI date suffix
    ("claude-opus-5-5@20260922", "claude-opus-5-5"),     # Vertex spelling of one
    ("openai/gpt-prefixed", "openai/gpt-prefixed"),      # a prefixed key, as recorded
    ("claude-opus-5.5", "claude-opus-5-5"),              # `.` read as `-`
])
def test_exact_spellings_find_their_own_key(catalog, model, key):
    assert key_for(model) == (key, True)


@pytest.mark.parametrize("model", [
    # The bug that made this module switch catalogs: a prefix matcher priced
    # Opus 5.5 as Opus 5, at $5/$25 against $4/$20.
    "claude-opus-5-6",
    "claude-opus-5.6",
    # ...and the same rule one family over.
    "gpt-6.1-sol",
    "gpt-6-1-sol",
])
def test_a_further_version_number_never_falls_back_to_its_predecessor(catalog, model):
    assert pricing.resolve(model) is None


def test_opus_5_5_is_priced_as_itself_not_as_opus_5(catalog):
    rates = pricing.rates_from_catalog("claude-opus-5-5")
    assert (rates[0].input_mtok, rates[0].output_mtok, rates[0].cache_read_mtok) == (4, 20, 0.2)


@pytest.mark.parametrize("model, key", [
    ("claude-opus-5-thinking", "claude-opus-5"),
    ("anthropic/claude-opus-5-5-thinking", "claude-opus-5-5"),
    ("gpt-5.3-codex-high", "gpt-5.3-codex"),
])
def test_a_fuzzy_match_takes_the_longest_key_and_says_it_was_fuzzy(catalog, model, key):
    """`gpt-5.3-codex-high` contains both `gpt-5` and `gpt-5.3-codex`; the
    longer one is the nearer relative."""
    assert key_for(model) == (key, False)


@pytest.mark.parametrize("model", ["o1-mini", "claude-opus-5-fast", "gpt-5-pro"])
def test_a_size_or_speed_tier_is_a_different_model_not_a_relative(catalog, model):
    """o1-mini costs a thirteenth of o1; pricing it as o1 is not an estimate."""
    assert pricing.resolve(model) is None


@pytest.mark.parametrize("model", [
    "muse-spark-1.3-contributor-free",
    "gpt-6-sol-free",
    "openrouter/claude-opus-5:free",
])
def test_a_free_tier_is_never_priced_as_the_paid_model(catalog, model):
    assert pricing.resolve(model) is None
    assert pricing.rates_from_catalog(model) == []


def test_litellm_beats_models_dev_on_a_fuzzy_match(catalog):
    """ccusage consults its primary map before the fallback, so a longer
    models.dev key loses a fuzzy match to a LiteLLM one -- but still wins
    when it is named exactly."""
    doc = json.loads(json.dumps(CATALOG))
    doc["models"]["claude-opus-5-thinking"] = {
        "source": "models.dev", "upstream": "reseller/claude-opus-5-thinking",
        "provider": "reseller", "trust": "reseller", "clauses": [clause(99, 99)]}
    catalog.write_text(json.dumps(doc))
    pricing.catalog.cache_clear()
    assert key_for("claude-opus-5-thinking-high") == ("claude-opus-5", False)
    assert key_for("claude-opus-5-thinking") == ("claude-opus-5-thinking", True)


def test_a_models_dev_entry_says_where_it_came_from(catalog):
    rates = pricing.rates_from_catalog("muse-spark-1.3")
    assert rates[0].origin == pricing.MODELS_DEV
    assert rates[0].matched_id == "models.dev:meta/muse-spark-1.3"
    assert "not in LiteLLM" in rates[0].note


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


def test_a_catalog_in_the_old_genai_shape_reads_as_empty_not_as_wrong(tmp_path, monkeypatch):
    """A stale checkout's snapshot must not be half-understood."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"source": {"repo": "pydantic/genai-prices"},
                                "provider_order": ["anthropic"],
                                "models": [{"provider": "anthropic", "id": "claude-opus-5",
                                            "match": {"starts_with": "claude-opus-5"},
                                            "clauses": [clause(5, 25)]}]}))
    monkeypatch.setattr(pricing, "CATALOG_PATH", path)
    pricing.catalog.cache_clear()
    try:
        assert pricing.resolve("claude-opus-5-5") is None
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


def test_sync_writes_every_dated_rate_for_models_in_use(conn, catalog, overrides):
    add_event(conn, "claude-test")
    add_event(conn, "gpt-5.3-codex")
    r = pricing.sync(conn)
    assert sorted(r.priced) == ["claude-test", "gpt-5.3-codex"]
    assert r.unpriced == [] and r.rows_written == 3  # two dated + one
    rows = conn.execute("SELECT model, effective_from FROM model_price ORDER BY 1, 2").fetchall()
    assert [tuple(x) for x in rows] == [
        ("claude-test", 0), ("claude-test", JUNE), ("gpt-5.3-codex", 0)]


def test_sync_records_which_catalog_entry_priced_a_model(conn, catalog, overrides):
    add_event(conn, "claude-opus-5-5-20260922")
    pricing.sync(conn)
    row = conn.execute("SELECT matched_id, origin, cache_write_1h_mtok FROM model_price"
                       ).fetchone()
    assert row["matched_id"] == "litellm:claude-opus-5-5"
    assert row["origin"] == pricing.LITELLM
    assert row["cache_write_1h_mtok"] == 8.0


def test_sync_is_idempotent(conn, catalog, overrides):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    before = conn.execute("SELECT count(*) FROM model_price").fetchone()[0]
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == before


def test_sync_reports_a_model_it_cannot_price(conn, catalog, overrides):
    add_event(conn, "llama-9")
    add_event(conn, "x-preview-f-free")
    r = pricing.sync(conn)
    assert sorted(r.unpriced) == ["llama-9", "x-preview-f-free"] and r.priced == []


def test_sync_drops_a_catalog_rate_that_upstream_withdrew(conn, catalog, overrides):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 2

    shrunk = json.loads(json.dumps(CATALOG))
    shrunk["models"]["claude-test"]["clauses"] = shrunk["models"]["claude-test"]["clauses"][:1]
    catalog.write_text(json.dumps(shrunk))
    pricing.catalog.cache_clear()
    pricing.sync(conn)
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 1


def test_sync_drops_the_rate_of_a_model_the_catalog_no_longer_prices(conn, catalog, overrides):
    """A row the old prefix matcher wrote for `claude-opus-5-5` (Opus 5's
    rates) must not survive the sync that can no longer justify it."""
    add_event(conn, "claude-opus-5-6")
    conn.execute("INSERT INTO model_price (model, effective_from, input_mtok, output_mtok,"
                 " currency, origin, matched_id, updated_at) VALUES"
                 " ('claude-opus-5-6', 0, 5, 25, 'USD', 'genai-prices',"
                 " 'anthropic/claude-opus-5', 0)")
    r = pricing.sync(conn)
    assert r.unpriced == ["claude-opus-5-6"]
    assert conn.execute("SELECT count(*) FROM model_price").fetchone()[0] == 0


def test_sync_never_overwrites_a_manual_rate(conn, catalog, overrides):
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


def test_a_manual_rate_survives_for_a_model_the_catalog_cannot_price(conn, catalog, overrides):
    add_event(conn, "muse-spark-1.3-contributor-free")
    pricing.set_price(conn, "muse-spark-1.3-contributor-free", input_mtok=0, output_mtok=0)
    r = pricing.sync(conn)
    assert r.priced == ["muse-spark-1.3-contributor-free"] and r.unpriced == []
    assert conn.execute("SELECT origin FROM model_price").fetchone()[0] == "manual"


def test_a_manual_rate_can_be_cleared_back_to_the_catalog(conn, catalog, overrides):
    add_event(conn, "claude-test")
    pricing.set_price(conn, "claude-test", input_mtok=1.23)
    assert pricing.clear_price(conn, "claude-test") == 1
    pricing.sync(conn)
    row = conn.execute(
        "SELECT input_mtok, origin FROM model_price WHERE model = 'claude-test'"
        " AND effective_from = 0").fetchone()
    assert row["input_mtok"] == 3.0 and row["origin"] == pricing.LITELLM


def test_clear_leaves_catalog_rows_alone(conn, catalog, overrides):
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


def test_approximations_names_a_fuzzy_match_and_only_that(conn, catalog, overrides):
    """A near relative is defensible; invisible it is not. A provider prefix
    or a date suffix is the same model, not a relative."""
    add_event(conn, "claude-opus-5-thinking")
    add_event(conn, "claude-opus-5-5-20260922")
    add_event(conn, "anthropic/claude-opus-5")
    add_event(conn, "muse-spark-1.3")
    pricing.sync(conn)
    assert pricing.approximations(conn) == [
        ("claude-opus-5-thinking", "litellm:claude-opus-5")]


def test_approximations_still_reads_rows_an_older_catalog_wrote(conn, catalog):
    conn.execute("INSERT INTO model_price (model, effective_from, input_mtok, currency,"
                 " origin, matched_id, updated_at) VALUES"
                 " ('claude-fable-5-1', 0, 10, 'USD', 'genai-prices',"
                 " 'anthropic/claude-fable-5', 0)")
    assert pricing.approximations(conn) == [("claude-fable-5-1", "anthropic/claude-fable-5")]


def test_a_platform_key_for_the_same_model_is_not_an_approximation(conn, catalog):
    """A retired Claude model priced from Bedrock's id for it is still itself."""
    conn.execute("INSERT INTO model_price (model, effective_from, input_mtok, currency,"
                 " origin, matched_id, updated_at) VALUES"
                 " ('claude-3-5-haiku-20241022', 0, 0.8, 'USD', 'litellm',"
                 " 'litellm:anthropic.claude-3-5-haiku-20241022-v1:0', 0)")
    assert pricing.approximations(conn) == []


def test_a_manual_rate_is_never_reported_as_an_approximation(conn, catalog):
    pricing.set_price(conn, "whatever", input_mtok=1.0)
    assert pricing.approximations(conn) == []


def test_models_in_use_reads_every_model_any_event_names(conn, catalog):
    add_event(conn, "claude-test")
    add_event(conn, "gpt-5.3-codex")
    add_event(conn, None)
    assert pricing.models_in_use(conn) == ["claude-test", "gpt-5.3-codex"]


def test_load_rates_groups_by_model_oldest_first(conn, catalog, overrides):
    add_event(conn, "claude-test")
    pricing.sync(conn)
    loaded = pricing.load_rates(conn)
    assert [r.effective_from for r in loaded["claude-test"]] == [0, JUNE]


# --- the shipped snapshot ----------------------------------------------
#
# These do NOT take the `catalog` fixture: they are assertions about the file
# this repo ships, and running them against the miniature test catalog would
# prove nothing about what a user gets.


def test_the_shipped_catalog_is_readable_and_traceable():
    """A truncated or hand-edited file would make every model silently
    unpriced; one without provenance could not be audited."""
    doc = json.loads(pricing.CATALOG_PATH.read_text())
    src = doc["source"]
    assert src["repo"] == "BerriAI/litellm" and len(src["commit"]) == 40
    assert src["fallback"]["name"] == "models.dev" and len(src["fallback"]["sha256"]) == 64
    assert len(doc["models"]) > 100
    for key, entry in doc["models"].items():
        assert entry["source"] in (pricing.LITELLM, pricing.MODELS_DEV), key
        assert entry["upstream"] and entry["clauses"], key
        for c in entry["clauses"]:
            # Per million tokens. A per-token rate (4e-06) slipping through
            # would price every model at a millionth of its cost.
            assert c["input_mtok"] is None or c["input_mtok"] >= 0.001, key
        assert not pricing.is_free_tier(key), key


@pytest.mark.parametrize("model, expected", [
    # (input, output, cache read, 5m write, 1h write) per MTok. The Anthropic
    # rows are the claude-api skill's table; 1h is LiteLLM's published rate.
    ("claude-opus-5-5", (4, 20, 0.2, 5, 8)),
    ("claude-opus-5", (5, 25, 0.5, 6.25, 10)),
    ("claude-fable-5-1", (10, 50, 0.25, 12.5, 20)),
    ("claude-fable-5", (10, 50, 1, 12.5, 20)),
    ("claude-sonnet-5-5", (2, 10, 0.2, 2.5, 4)),
    ("gpt-6.1-sol", (2, 10, 0.1, 2.5, None)),
    ("gpt-6-sol", (2, 10, 0.2, 2.5, None)),
    ("gpt-6-astra", (10, 50, 1, 12.5, None)),
])
def test_the_shipped_catalog_prices_the_models_in_use(model, expected):
    pricing.catalog.cache_clear()
    hit = pricing.resolve(model)
    assert hit is not None and hit["exact"], model
    got = tuple(hit["clauses"][-1][f"{c}_mtok"] for c in pricing.COMPONENTS)
    assert got == expected


@pytest.mark.parametrize("model", ["x-preview-f-free", "muse-spark-1.3-contributor-free"])
def test_the_shipped_catalog_leaves_free_tiers_unpriced(model):
    pricing.catalog.cache_clear()
    assert pricing.resolve(model) is None


# --- the shipped corrections -------------------------------------------
#
# A rate correction that lives in one laptop's database is lost on the next
# machine and on the next rebuild. `price_overrides.json` is the layer that
# makes one durable, and these tests pin the two properties that make it safe:
# it beats the catalog, and a human still beats it.


def test_the_shipped_corrections_parse_and_carry_their_provenance():
    doc = json.loads(pricing.OVERRIDES_PATH.read_text())
    for entry in doc["models"]:
        assert entry["id"] and entry["clauses"]
        # Every one of these is a claim about someone else's price list. It
        # is only defensible with a source and a date beside it.
        assert entry["source"].startswith("https://")
        assert entry["checked"] and entry["why"]
    # A retired correction says why it could go, so nobody re-adds it blind.
    for entry in doc.get("retired", []):
        assert entry["id"] and entry["corrected"] and entry["why_retired"]
        assert entry["checked"]


def test_no_shipped_correction_is_redundant_with_the_catalog():
    """An override the catalog agrees with is only a way to go stale."""
    pricing.catalog.cache_clear()
    pricing.override_catalog.cache_clear()
    for entry in json.loads(pricing.OVERRIDES_PATH.read_text())["models"]:
        assert not pricing._override_is_redundant(entry["id"], entry), entry["id"]


def test_a_correction_beats_the_catalog(conn, catalog, overrides):
    overrides({**FABLE_51_FIX, "id": "claude-opus-5-5",
               "clauses": [clause(4, 20, 0.11, 5, 8)]})
    add_event(conn, "claude-opus-5-5")
    pricing.sync(conn)
    row = conn.execute(
        "SELECT cache_read_mtok, origin, note FROM model_price WHERE model = ?",
        ("claude-opus-5-5",)).fetchone()
    assert row["cache_read_mtok"] == 0.11
    assert row["origin"] == pricing.OVERRIDE
    assert row["note"], "a correction must carry its reason into the table"


def test_a_correction_never_reaches_a_model_it_does_not_name(catalog, overrides):
    """`claude-fable-5-1` and `claude-fable-5` differ only in cache reads; a
    correction for one that caught the other reintroduces the error."""
    overrides(FABLE_51_FIX)
    assert pricing.resolve_override("claude-fable-5-1-20261001") is not None
    assert pricing.resolve_override("anthropic/claude-fable-5.1") is not None
    for other in ("claude-fable-5", "claude-fable-5-1-thinking", "claude-fable-5-10"):
        assert pricing.resolve_override(other) is None, other


def test_a_human_still_beats_a_correction(conn, catalog, overrides):
    overrides(FABLE_51_FIX)
    add_event(conn, "claude-fable-5-1")
    pricing.set_price(conn, "claude-fable-5-1", cache_read_mtok=0.11)
    pricing.sync(conn)
    row = conn.execute(
        "SELECT cache_read_mtok, origin FROM model_price WHERE model = ?",
        ("claude-fable-5-1",)).fetchone()
    assert row["cache_read_mtok"] == 0.11 and row["origin"] == pricing.MANUAL


def test_a_corrected_model_is_not_reported_as_an_approximation(conn, catalog, overrides):
    overrides(FABLE_51_FIX)
    add_event(conn, "claude-fable-5-1")
    pricing.sync(conn)
    assert pricing.approximations(conn) == []


def test_sync_reports_which_models_a_correction_priced(conn, catalog, overrides):
    overrides(FABLE_51_FIX)
    add_event(conn, "claude-fable-5-1")
    add_event(conn, "claude-test")
    r = pricing.sync(conn)
    assert r.overridden == ["claude-fable-5-1"]
    assert set(r.priced) == {"claude-fable-5-1", "claude-test"}
    assert r.redundant == []  # the catalog has no claude-fable-5-1 of its own


def test_sync_flags_a_correction_upstream_has_caught_up_with(conn, catalog, overrides):
    """An override the catalog now agrees with is not a correction any more,
    only a way to go stale. Disagreeing is not catching up."""
    overrides(FABLE_51_FIX, {**FABLE_51_FIX, "id": "claude-opus-5",
                             "clauses": [clause(5, 25, 0.25, 6.25, 10)]})
    caught_up = json.loads(json.dumps(CATALOG))
    caught_up["models"]["claude-fable-5-1"] = litellm(clause(10.0, 50.0, 0.25, 12.5, 20.0))
    catalog.write_text(json.dumps(caught_up))
    pricing.catalog.cache_clear()

    add_event(conn, "claude-fable-5-1")
    add_event(conn, "claude-opus-5")
    r = pricing.sync(conn)
    assert r.redundant == ["claude-fable-5-1"]
    # Still applied -- flagging is not the same as silently standing down.
    assert sorted(r.overridden) == ["claude-fable-5-1", "claude-opus-5"]
