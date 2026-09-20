"""Derive `event_cost` from `event` and `model_price`.

Same shape as `derive.py`: a pure function of tables it does not own, cleared
and rebuilt whole, so the answer never depends on the order things happened.
`derive.py` owns `span`; this owns `event_cost`; neither touches the other.

Four decisions, each one a way this could have been quietly wrong.

**Cost is a list price, not a bill.** Nothing in these logs knows what the
user actually paid. A Claude Max or ChatGPT Plus subscription bills a flat
monthly fee no matter how many tokens run through it, and opencode's own
`cost` column is 0 on every row here for exactly that reason. What this module
computes is *what the same traffic would have cost at published API rates* --
a useful number for comparing projects, models and months, and a wrong number
to put in an invoice. Every surface that prints it has to say so.

**Cache reads are priced separately, and they dominate.** On the measured
corpus, cache reads are 9.85 billion tokens against 3.3 million fresh input
tokens -- three thousand to one. Folding them into `input` at the input rate
would overstate the total by more than an order of magnitude; ignoring them
would understate it by most of the bill. They get their own rate, as do cache
writes, which are charged at a premium rather than a discount.

**A model with no price contributes nothing and is counted.** It gets no row
in `event_cost` and a row in `event_unpriced` instead, so every total can say
what it could not see. A zero would be indistinguishable from a model that is
genuinely free, and the difference is the entire trustworthiness of the
number. An event can land in both tables: a model priced for input and output
but not for cache writes is charged for what is known and recorded for what
is not.

**Codex records usage on events that name no model.** Measured 2026-09-20:
12,412 token-bearing Codex events carry `model IS NULL`, and the model lives
on other events in the same thread. Each thread is therefore walked in time
order carrying the last model seen forward, and events priced that way are
flagged `attributed = 1` so a reader can ask how much of a total rests on
the inference.

Getting a model and getting a price are different things, and the split
matters: 11,097 of those events were priced this way, 1,221 got a model that
has no rate on file (`gpt-6-astra`) and 46 have no model anywhere earlier in
their thread. The last two land in `event_unpriced` under `no_rate` and
`no_model` respectively.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from cc_insights import pricing
from cc_insights.pricing import COMPONENTS, Rates

#: Integer nano-currency-units per unit: costs are stored as integers so that
#: SUM() over a hundred thousand rows is exact rather than float-drifted.
NANO = 1_000_000_000

#: Rows buffered before a write.
_CHUNK = 5_000

#: Ids per `IN (...)` list when re-pricing a scope, matching derive.py.
_ID_CHUNK = 400

#: Why an event's tokens went unpriced. Stored, not inferred later.
NO_MODEL = "no_model"
NO_RATE = "no_rate"
NO_COMPONENT = "no_component"

#: event columns -> rate component.
_TOKEN_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
}


@dataclass(slots=True)
class Unpriced:
    """Tokens that could not be priced, and why -- never silently dropped."""

    #: model -> tokens, for models with no rate at all at that time.
    by_model: dict[str, int] = field(default_factory=dict)
    #: tokens whose model is known and priced, but whose component rate is not.
    by_component: dict[str, int] = field(default_factory=dict)
    #: token-bearing events whose model could not be determined at all.
    unknown_model_events: int = 0
    unknown_model_tokens: int = 0

    @property
    def tokens(self) -> int:
        return (sum(self.by_model.values()) + sum(self.by_component.values())
                + self.unknown_model_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens,
            "byModel": dict(sorted(self.by_model.items(), key=lambda kv: -kv[1])),
            "byComponent": dict(self.by_component),
            "unknownModelEvents": self.unknown_model_events,
            "unknownModelTokens": self.unknown_model_tokens,
        }


@dataclass(slots=True)
class CostResult:
    events: int = 0
    attributed: int = 0
    total_nano: int = 0
    by_component: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)
    unpriced: Unpriced = field(default_factory=Unpriced)
    currency: str = "USD"
    duration_s: float = 0.0

    @property
    def total(self) -> float:
        """The total in currency units. Display only -- storage stays integer."""
        return self.total_nano / NANO

    def as_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "attributed": self.attributed,
            "total": round(self.total, 6),
            "currency": self.currency,
            "byComponent": {k: round(v / NANO, 6) for k, v in self.by_component.items()},
            "byModel": {
                k: round(v / NANO, 6)
                for k, v in sorted(self.by_model.items(), key=lambda kv: -kv[1])
            },
            "unpriced": self.unpriced.as_dict(),
            "durationS": round(self.duration_s, 3),
        }


def nano_for(tokens: int, rate_per_mtok: float) -> int:
    """Cost of `tokens` at a per-million rate, in nano-units, rounded once.

    Rounding at the event is deliberate: a per-event integer is what makes
    every later SUM exact. The worst-case error is half a nano-unit per event,
    or about 0.00005 currency units across the whole measured corpus.
    """
    return round(tokens * rate_per_mtok * NANO / 1_000_000)


def _chunks(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for i in range(0, len(values), _ID_CHUNK):
        yield values[i : i + _ID_CHUNK]


_TOKEN_EVENT_SQL = """
    SELECT e.id, e.session_id, e.thread_id, e.ts, e.model,
           e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_tokens
    FROM e_scope e
    WHERE e.model IS NOT NULL
       OR e.input_tokens IS NOT NULL OR e.output_tokens IS NOT NULL
       OR e.cache_read_tokens IS NOT NULL OR e.cache_write_tokens IS NOT NULL
    ORDER BY e.thread_id, e.ts, e.ordinal, e.id
"""


def _token_events(
    conn: sqlite3.Connection, scope: Sequence[str] | None
) -> Iterator[sqlite3.Row]:
    """Every event that carries tokens or names a model, in thread-time order.

    Both kinds are needed: naming a model is what lets a *later* token-bearing
    event in the same thread be priced. Ordering by thread then time is what
    makes that carry-forward well defined.

    A scope is a list of session ids -- what `cci watch` passes after an
    incremental ingest, so a two-second cycle re-prices the handful of
    sessions that moved instead of all 66,000 events. Scoping by session is
    safe precisely because the only thing carried between events is the
    model, and that never crosses a thread, let alone a session.
    """
    if scope is None:
        yield from conn.execute(_TOKEN_EVENT_SQL.replace("e_scope", "event"))
        return
    for chunk in _chunks(list(scope)):
        marks = ",".join("?" * len(chunk))
        sql = _TOKEN_EVENT_SQL.replace(
            "e_scope", f"(SELECT * FROM event WHERE session_id IN ({marks}))"
        )
        yield from conn.execute(sql, tuple(chunk))


def _price_event(
    row: sqlite3.Row, rates: Rates
) -> tuple[dict[str, int], dict[str, int]]:
    """(nano cost per component, unpriced tokens per component)."""
    costs: dict[str, int] = {}
    missing: dict[str, int] = {}
    for component, column in _TOKEN_COLUMNS.items():
        tokens = row[column] or 0
        if tokens <= 0:
            continue
        rate = rates.rate(component)
        if rate is None:
            missing[component] = tokens
        else:
            costs[component] = nano_for(tokens, rate)
    return costs, missing


def derive_costs(
    conn: sqlite3.Connection, *, session_ids: Sequence[str] | None = None
) -> CostResult:
    """Rebuild the cost tables. Returns what was and was not priced.

    With `session_ids`, only those sessions are re-priced and every other
    session's rows are left alone; the returned `CostResult` then describes
    the scope, not the corpus. With `None` the whole database is rebuilt,
    which is what `cci cost` does and what a price change requires.

    Runs in one transaction: a half-priced table would make a dashboard show
    a total that is neither the old answer nor the new one.
    """
    scope = list(dict.fromkeys(session_ids)) if session_ids is not None else None
    started = time.perf_counter()
    rates_by_model = pricing.load_rates(conn)
    result = CostResult()
    currencies: set[str] = set()

    conn.execute("BEGIN")
    try:
        _clear(conn, scope)
        priced_batch: list[tuple] = []
        unpriced_batch: list[tuple] = []
        last_thread: str | None = None
        carried: str | None = None

        def note_unpriced(row, model, attributed, tokens, reason) -> None:
            unpriced_batch.append((
                row["id"], row["session_id"], row["thread_id"], row["ts"],
                model, attributed, tokens, reason,
            ))
            # Flushed here rather than beside the priced flush below, which
            # sits after a `continue` and so never ran on a corpus where
            # nothing could be priced -- exactly the corpus whose unpriced
            # batch grows without bound.
            if len(unpriced_batch) >= _CHUNK:
                _write_unpriced(conn, unpriced_batch)
                unpriced_batch.clear()

        for row in _token_events(conn, scope):
            if row["thread_id"] != last_thread:
                last_thread, carried = row["thread_id"], None
            if row["model"]:
                carried = row["model"]

            tokens = sum(row[c] or 0 for c in _TOKEN_COLUMNS.values())
            if tokens <= 0:
                continue

            model = row["model"] or carried
            attributed = 1 if row["model"] is None else 0
            if model is None:
                result.unpriced.unknown_model_events += 1
                result.unpriced.unknown_model_tokens += tokens
                note_unpriced(row, None, 0, tokens, NO_MODEL)
                continue

            rates = pricing.rates_at(rates_by_model.get(model, ()), row["ts"])
            if rates is None:
                result.unpriced.by_model[model] = (
                    result.unpriced.by_model.get(model, 0) + tokens
                )
                note_unpriced(row, model, attributed, tokens, NO_RATE)
                continue

            costs, missing = _price_event(row, rates)
            if missing:
                for component, n in missing.items():
                    result.unpriced.by_component[component] = (
                        result.unpriced.by_component.get(component, 0) + n
                    )
                note_unpriced(row, model, attributed, sum(missing.values()), NO_COMPONENT)
            if not costs:
                continue

            currencies.add(rates.currency)
            total = sum(costs.values())
            result.events += 1
            result.attributed += attributed
            result.total_nano += total
            result.by_model[model] = result.by_model.get(model, 0) + total
            for component, n in costs.items():
                result.by_component[component] = result.by_component.get(component, 0) + n

            priced_batch.append((
                row["id"], row["session_id"], row["thread_id"], row["ts"], model,
                attributed, rates.effective_from,
                costs.get("input", 0), costs.get("output", 0),
                costs.get("cache_read", 0), costs.get("cache_write", 0),
            ))
            if len(priced_batch) >= _CHUNK:
                _write_priced(conn, priced_batch)
                priced_batch = []
        _write_priced(conn, priced_batch)
        _write_unpriced(conn, unpriced_batch)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    # Mixing currencies would make the total meaningless, so say which one it
    # is rather than adding euros to dollars behind a "$".
    result.currency = currencies.pop() if len(currencies) == 1 else "mixed"
    result.duration_s = time.perf_counter() - started
    return result


def _clear(conn: sqlite3.Connection, scope: Sequence[str] | None) -> None:
    """Drop the cost rows in scope so they can be rebuilt."""
    if scope is None:
        conn.execute("DELETE FROM event_cost")
        conn.execute("DELETE FROM event_unpriced")
        return
    for chunk in _chunks(list(scope)):
        marks = ",".join("?" * len(chunk))
        conn.execute(f"DELETE FROM event_cost WHERE session_id IN ({marks})", tuple(chunk))
        conn.execute(f"DELETE FROM event_unpriced WHERE session_id IN ({marks})", tuple(chunk))


def _write_priced(conn: sqlite3.Connection, batch: Sequence[tuple]) -> None:
    if batch:
        conn.executemany(
            """INSERT INTO event_cost
                 (event_id, session_id, thread_id, ts, model, attributed, price_from,
                  input_nano, output_nano, cache_read_nano, cache_write_nano)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )


def _write_unpriced(conn: sqlite3.Connection, batch: Sequence[tuple]) -> None:
    if batch:
        conn.executemany(
            """INSERT INTO event_unpriced
                 (event_id, session_id, thread_id, ts, model, attributed, tokens, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            batch,
        )


__all__ = [
    "COMPONENTS", "CostResult", "NANO", "NO_COMPONENT", "NO_MODEL", "NO_RATE",
    "Unpriced", "derive_costs", "nano_for",
]
