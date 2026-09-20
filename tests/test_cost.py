"""Tests for cost derivation.

Three of these are the reason the module exists. `test_cache_reads_are_priced
_at_their_own_rate` pins the component split -- the measured corpus is 3,000
cache-read tokens for every fresh input token, so folding them together is
wrong by an order of magnitude. `test_an_unpriced_model_costs_nothing_and_says
_so` pins the rule that unknown is not zero. `test_a_model_is_carried_forward
_within_a_thread_only` pins the Codex attribution, which is the only place
this module infers anything.
"""

import sqlite3

import pytest

from cc_insights import cost, pricing

HOUR = 3_600_000
T0 = 1_780_000_000_000
LATER = pricing.day_to_ms("2026-06-01")


def seed(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO host (host_id, hostname, first_seen, last_seen)"
                 " VALUES ('h', 'H', 0, 0)")
    for sid, source in (("s1", "claude_code"), ("s2", "codex")):
        conn.execute("INSERT INTO session (id, native_id, source, host_id, started_at,"
                     " ended_at) VALUES (?, ?, ?, 'h', 0, 0)", (sid, f"n-{sid}", source))
    for tid, sid in (("t1", "s1"), ("t2", "s1"), ("t3", "s2")):
        conn.execute("INSERT INTO thread (id, native_id, session_id, started_at, ended_at)"
                     " VALUES (?, ?, ?, 0, 0)", (tid, f"n-{tid}", sid))


def event(conn, eid, thread, ts, *, model=None, i=0, o=0, cr=0, cw=0, ordinal=0):
    sid = {"t1": "s1", "t2": "s1", "t3": "s2"}[thread]
    conn.execute(
        "INSERT INTO event (id, session_id, thread_id, native_event_id, ts, ordinal, kind,"
        " model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?)",
        (eid, sid, thread, f"n-{eid}", ts, ordinal, model,
         i or None, o or None, cr or None, cw or None))


def price(conn, model, *, i=None, o=None, cr=None, cw=None, since=0, origin="genai-prices"):
    conn.execute(
        "INSERT INTO model_price (model, effective_from, input_mtok, output_mtok,"
        " cache_read_mtok, cache_write_mtok, currency, origin, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, 0)", (model, since, i, o, cr, cw, origin))


@pytest.fixture
def seeded(conn):
    seed(conn)
    return conn


def rows(conn, table):
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY event_id")]


# --- arithmetic ---------------------------------------------------------


@pytest.mark.parametrize("tokens, rate, expected_nano", [
    (1_000_000, 5.0, 5 * cost.NANO),
    (1, 5.0, 5_000),                 # one token at $5/MTok = 5 micro-dollars
    (0, 5.0, 0),
    (333, 0.3, 99_900),
])
def test_nano_for(tokens, rate, expected_nano):
    assert cost.nano_for(tokens, rate) == expected_nano


def test_totals_are_exact_over_many_events(seeded):
    """Integer nano-units, not floats: a hundred thousand additions of 0.1
    must still be 10,000, which binary floating point does not promise."""
    price(seeded, "m", i=1.0)
    for n in range(2000):
        event(seeded, f"e{n}", "t1", T0 + n, model="m", i=100_000, ordinal=n)
    r = cost.derive_costs(seeded)
    assert r.total_nano == 2000 * cost.nano_for(100_000, 1.0)
    assert r.total == 200.0


# --- the component split ------------------------------------------------


def test_cache_reads_are_priced_at_their_own_rate(seeded):
    """On the real corpus cache reads outnumber fresh input tokens 3,000 to
    one. Priced at the input rate this total would be wrong by an order of
    magnitude; dropped, it would miss most of the bill."""
    price(seeded, "m", i=5.0, o=25.0, cr=0.5, cw=6.25)
    event(seeded, "e1", "t1", T0, model="m", i=1_000_000, o=1_000_000,
          cr=1_000_000, cw=1_000_000)
    r = cost.derive_costs(seeded)
    assert r.by_component == {
        "input": 5 * cost.NANO, "output": 25 * cost.NANO,
        "cache_read": int(0.5 * cost.NANO), "cache_write": int(6.25 * cost.NANO),
    }
    assert r.total == 36.75


def test_every_component_lands_on_its_own_column(seeded):
    price(seeded, "m", i=1.0, o=2.0, cr=3.0, cw=4.0)
    event(seeded, "e1", "t1", T0, model="m", i=1_000_000, o=1_000_000,
          cr=1_000_000, cw=1_000_000)
    cost.derive_costs(seeded)
    row = rows(seeded, "event_cost")[0]
    assert (row["input_nano"], row["output_nano"],
            row["cache_read_nano"], row["cache_write_nano"]) == (
        1 * cost.NANO, 2 * cost.NANO, 3 * cost.NANO, 4 * cost.NANO)


# --- dated prices -------------------------------------------------------


def test_each_event_is_priced_at_the_rate_of_its_own_day(seeded):
    price(seeded, "m", i=2.0, since=0)
    price(seeded, "m", i=3.0, since=LATER)
    event(seeded, "e1", "t1", LATER - HOUR, model="m", i=1_000_000)
    event(seeded, "e2", "t1", LATER + HOUR, model="m", i=1_000_000, ordinal=1)
    cost.derive_costs(seeded)
    got = {r["event_id"]: (r["input_nano"], r["price_from"]) for r in rows(seeded, "event_cost")}
    assert got["e1"] == (2 * cost.NANO, 0)
    assert got["e2"] == (3 * cost.NANO, LATER)


def test_an_event_before_the_earliest_rate_is_unpriced(seeded):
    """Not priced at a rate that did not exist yet."""
    price(seeded, "m", i=3.0, since=LATER)
    event(seeded, "e1", "t1", LATER - HOUR, model="m", i=1_000)
    r = cost.derive_costs(seeded)
    assert r.total_nano == 0
    assert rows(seeded, "event_unpriced")[0]["reason"] == cost.NO_RATE


# --- unknown is not zero ------------------------------------------------


def test_an_unpriced_model_costs_nothing_and_says_so(seeded):
    event(seeded, "e1", "t1", T0, model="mystery", i=1_000, o=500)
    r = cost.derive_costs(seeded)
    assert r.total_nano == 0 and rows(seeded, "event_cost") == []
    assert r.unpriced.by_model == {"mystery": 1_500}
    unp = rows(seeded, "event_unpriced")[0]
    assert (unp["model"], unp["tokens"], unp["reason"]) == ("mystery", 1_500, cost.NO_RATE)


def test_a_partly_priced_model_lands_in_both_tables(seeded):
    """OpenAI publishes no cache-write rate. Charge what is known, record the
    rest -- not silently zero, and not silently dropping the whole event."""
    price(seeded, "m", i=1.0, o=2.0, cr=3.0, cw=None)
    event(seeded, "e1", "t1", T0, model="m", i=1_000_000, cw=777)
    r = cost.derive_costs(seeded)
    assert r.total_nano == 1 * cost.NANO
    assert r.unpriced.by_component == {"cache_write": 777}
    unp = rows(seeded, "event_unpriced")[0]
    assert (unp["tokens"], unp["reason"]) == (777, cost.NO_COMPONENT)
    assert len(rows(seeded, "event_cost")) == 1


def test_an_event_with_no_tokens_is_neither_priced_nor_reported(seeded):
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t1", T0, model="m")
    r = cost.derive_costs(seeded)
    assert r.events == 0 and rows(seeded, "event_unpriced") == []


# --- Codex model attribution --------------------------------------------


def test_a_model_is_carried_forward_within_a_thread_only(seeded):
    """Codex records usage on events that name no model; the model is on other
    events in the same thread. Carrying it across threads would attribute one
    thread's tokens to another thread's model."""
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t3", T0, model="m", ordinal=0)
    event(seeded, "e2", "t3", T0 + 1, i=1_000_000, ordinal=1)      # attributed
    event(seeded, "e3", "t1", T0 + 2, i=2_000_000, ordinal=2)      # different thread
    r = cost.derive_costs(seeded)

    priced = {x["event_id"]: x for x in rows(seeded, "event_cost")}
    assert priced["e2"]["model"] == "m" and priced["e2"]["attributed"] == 1
    assert "e3" not in priced
    assert r.attributed == 1
    assert r.unpriced.unknown_model_events == 1
    assert rows(seeded, "event_unpriced")[0]["reason"] == cost.NO_MODEL


def test_an_event_that_names_its_own_model_is_not_marked_attributed(seeded):
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t1", T0, model="m", i=1_000)
    cost.derive_costs(seeded)
    assert rows(seeded, "event_cost")[0]["attributed"] == 0


def test_attribution_runs_forward_in_time_not_backward(seeded):
    """A token event before any model in its thread has nothing to inherit."""
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t3", T0, i=1_000, ordinal=0)
    event(seeded, "e2", "t3", T0 + 1, model="m", ordinal=1)
    r = cost.derive_costs(seeded)
    assert r.unpriced.unknown_model_events == 1 and r.total_nano == 0


# --- rebuild semantics --------------------------------------------------


def test_deriving_twice_changes_nothing(seeded):
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t1", T0, model="m", i=1_000)
    event(seeded, "e2", "t1", T0 + 1, model="x", i=1_000, ordinal=1)
    first = cost.derive_costs(seeded).as_dict()
    second = cost.derive_costs(seeded).as_dict()
    assert first["total"] == second["total"]
    assert len(rows(seeded, "event_cost")) == 1
    assert len(rows(seeded, "event_unpriced")) == 1


def test_a_price_change_is_picked_up_by_rederiving(seeded):
    """The reason this table is rebuilt whole rather than accumulated."""
    price(seeded, "m", i=1.0)
    event(seeded, "e1", "t1", T0, model="m", i=1_000_000)
    assert cost.derive_costs(seeded).total == 1.0

    seeded.execute("DELETE FROM model_price")
    price(seeded, "m", i=10.0)
    assert cost.derive_costs(seeded).total == 10.0


def test_a_currency_mix_is_named_rather_than_added_up(seeded):
    price(seeded, "usd", i=1.0)
    seeded.execute("UPDATE model_price SET currency = 'EUR' WHERE model = 'usd'")
    price(seeded, "other", i=1.0)
    event(seeded, "e1", "t1", T0, model="usd", i=1_000)
    event(seeded, "e2", "t1", T0 + 1, model="other", i=1_000, ordinal=1)
    assert cost.derive_costs(seeded).currency == "mixed"


def test_an_empty_corpus_prices_to_zero_not_an_error(seeded):
    r = cost.derive_costs(seeded)
    assert r.total == 0 and r.events == 0 and r.unpriced.tokens == 0


# --- the report ---------------------------------------------------------


def test_the_result_reports_what_it_could_not_see(seeded):
    price(seeded, "m", i=1.0, o=None)
    event(seeded, "e1", "t1", T0, model="m", i=1_000, o=50)
    event(seeded, "e2", "t1", T0 + 1, model="ghost", i=9_000, ordinal=1)
    d = cost.derive_costs(seeded).as_dict()
    assert d["unpriced"]["byModel"] == {"ghost": 9_000}
    assert d["unpriced"]["byComponent"] == {"output": 50}
    assert d["unpriced"]["tokens"] == 9_050
