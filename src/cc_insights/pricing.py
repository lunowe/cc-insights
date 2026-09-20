"""The price table: what a model cost, on the day it ran.

This module owns `model_price` and nothing else. It does not touch events and
it does not compute money -- that is `cost.py`. The split matters because the
two change for different reasons: prices change when a vendor publishes,
costs change when logs arrive.

**A rate is a property of a model on a day, not of a model.** Claude Sonnet 5
went from $2/$10 to $3/$15 per MTok on 2026-09-01. Pricing August's traffic at
September's rate would quietly rewrite last month's total every time a vendor
moves a number, which is exactly the kind of drift that makes a dashboard
untrustworthy without ever looking broken. So rows are keyed
(model, effective_from) and an event is priced with the newest row not later
than the event.

**Where the numbers come from.** `model_prices.json`, a snapshot of the
pydantic/genai-prices catalog committed to this repo and refreshed by
`scripts/sync_prices.py`. Nothing here reaches the network: pricing is
arithmetic over a local file, and a tool that promises your logs stay on the
laptop should not phone out to do arithmetic.

**Matching is by the model string the log recorded.** Our sources write a bare
model name (`claude-opus-5`, `gpt-5.3-codex`), never a provider-qualified one,
so a string is matched against each provider's patterns in the catalog's
`provider_order` -- first-party vendors before resellers -- and the first hit
wins. The provider and catalog id that won are stored on the row, so a rate
that looks wrong can be traced to a specific upstream entry rather than argued
about.

**Three layers, and the more specific one wins.**

1. `model_prices.json` -- the catalog snapshot. Broad, maintained upstream,
   and matched by pattern, which is exactly why it is sometimes wrong about a
   model it has no entry for.
2. `price_overrides.json` -- corrections checked against the vendor's own
   pricing page, shipped with the code. This layer exists because a rate
   correction that lives in one laptop's database is lost on the next machine
   and on the next rebuild, and because the errors are not small: the catalog
   priced `claude-fable-5-1` as `claude-fable-5`, whose cache reads cost four
   times as much, which on the measured corpus was 21% of the total.
3. `origin = 'manual'` -- `cci price set`. A human beats both and `sync()`
   never touches it, the same rule `project_group.origin` follows. That is
   the escape hatch for what neither file can know: an enterprise discount,
   or a model that exists only inside one company.

An override is a full replacement, not a patch: reading one row tells you all
four rates a model was charged at, rather than sending you to a second file to
find out which of them came from where. The cost is that an override freezes
the rates it does not correct, so each one records `checked` and `source`, and
`sync()` says when upstream has caught up and the override can go.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

from cc_insights import db

#: The committed catalog snapshot. Rebuilt by `scripts/sync_prices.py`.
CATALOG_PATH = Path(__file__).resolve().parent / "model_prices.json"

#: Hand-checked corrections to it. Edited by a human, never by a script.
OVERRIDES_PATH = Path(__file__).resolve().parent / "price_overrides.json"

GENAI = "genai-prices"
#: Shipped correction to the catalog. Beats it; loses to MANUAL.
OVERRIDE = "override"
MANUAL = "manual"

#: Rate columns, in the order every report prints them.
COMPONENTS = ("input", "output", "cache_read", "cache_write")

_TIERED_NOTE = (
    "context-window tiers flattened to the base rate: the logs record tokens "
    "per request, not context length, so the tier cannot be chosen"
)


@dataclass(frozen=True, slots=True)
class Rates:
    """What one million tokens of each component cost, from `effective_from`.

    A None component is *not known*, which is not the same as free: `cost.py`
    reports tokens against a None rate as unpriced rather than as zero.
    """

    effective_from: int
    input_mtok: float | None = None
    output_mtok: float | None = None
    cache_read_mtok: float | None = None
    cache_write_mtok: float | None = None
    currency: str = "USD"
    origin: str = GENAI
    matched_id: str | None = None
    note: str | None = None

    def rate(self, component: str) -> float | None:
        return getattr(self, f"{component}_mtok")


# --------------------------------------------------------------------------
# the catalog snapshot
# --------------------------------------------------------------------------


def day_to_ms(day: str | None) -> int:
    """`YYYY-MM-DD` -> epoch ms at UTC midnight. None (or junk) -> 0.

    0 means "for as long as this table has records", which is what a vendor's
    current price means when no change is documented. UTC midnight is an
    approximation of a price change that a vendor announces as a date and
    rolls out in its own time zone; the error is bounded by a day, on a day
    the rate changed, which is the honest resolution available.
    """
    if not day:
        return 0
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return 0
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _matches(rule: Any, model: str) -> bool:
    """The catalog's matcher language: or / and / equals / starts_with / ..."""
    if not isinstance(rule, dict):
        return False
    if "or" in rule:
        return any(_matches(r, model) for r in rule["or"])
    if "and" in rule:
        return all(_matches(r, model) for r in rule["and"])
    if "equals" in rule:
        return model == rule["equals"]
    if "starts_with" in rule:
        return model.startswith(rule["starts_with"])
    if "ends_with" in rule:
        return model.endswith(rule["ends_with"])
    if "contains" in rule:
        return rule["contains"] in model
    if "regex" in rule:
        try:
            return re.search(rule["regex"], model) is not None
        except re.error:
            return False
    return False


@lru_cache(maxsize=1)
def catalog(path: str | None = None) -> dict[str, Any]:
    """The price snapshot, parsed once.

    A missing or unreadable snapshot is not fatal: every model is then
    unpriced and says so, which is a better failure than an exception out of
    `cci stats`.
    """
    target = Path(path) if path else CATALOG_PATH
    try:
        doc = json.loads(target.read_text())
    except (OSError, ValueError):
        return {"source": {}, "provider_order": [], "models": []}
    return doc


@lru_cache(maxsize=1)
def override_catalog(path: str | None = None) -> dict[str, Any]:
    """The shipped corrections, parsed once. Missing is legal and means none."""
    target = Path(path) if path else OVERRIDES_PATH
    try:
        doc = json.loads(target.read_text())
    except (OSError, ValueError):
        return {"models": []}
    return doc


def catalog_source() -> dict[str, Any]:
    """Provenance of the snapshot, for `cci price list` and `/api/meta`."""
    source = dict(catalog().get("source") or {})
    source["overrides"] = len(override_catalog().get("models", []))
    return source


def resolve(model: str) -> dict[str, Any] | None:
    """The catalog entry for a bare model string, or None.

    The snapshot is already ordered by `provider_order`, so the first entry
    whose matcher accepts the string is the preferred provider's.
    """
    for entry in catalog().get("models", []):
        if _matches(entry.get("match"), model):
            return entry
    return None


def resolve_override(model: str) -> dict[str, Any] | None:
    """The shipped correction for a model string, or None. First match wins."""
    for entry in override_catalog().get("models", []):
        if _matches(entry.get("match"), model):
            return entry
    return None


def _clauses_to_rates(entry: dict[str, Any], origin: str) -> list[Rates]:
    matched = f"{entry['provider']}/{entry['id']}"
    note = entry.get("why") if origin == OVERRIDE else None
    out = []
    for clause in entry.get("clauses", []):
        out.append(Rates(
            effective_from=day_to_ms(clause.get("start_date")),
            input_mtok=clause.get("input_mtok"),
            output_mtok=clause.get("output_mtok"),
            cache_read_mtok=clause.get("cache_read_mtok"),
            cache_write_mtok=clause.get("cache_write_mtok"),
            origin=origin,
            matched_id=matched,
            note=_TIERED_NOTE if clause.get("tiered") else note,
        ))
    out.sort(key=lambda r: r.effective_from)
    return out


def rates_from_catalog(model: str) -> list[Rates]:
    """Every dated rate known for a model, oldest first.

    A shipped override replaces the catalog outright for that model -- see the
    layering in the module docstring. The returned `Rates` carry the origin
    they came from, so the row written to `model_price` says which.
    """
    entry = resolve_override(model)
    if entry is not None:
        return _clauses_to_rates(entry, OVERRIDE)
    entry = resolve(model)
    if entry is None:
        return []
    return _clauses_to_rates(entry, GENAI)


# --------------------------------------------------------------------------
# the model_price table
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SyncResult:
    priced: list[str]
    unpriced: list[str]
    rows_written: int
    rows_kept_manual: int
    #: Models a shipped override priced instead of the catalog.
    overridden: list[str] = field(default_factory=list)
    #: Overrides upstream has caught up with -- the catalog now has an entry
    #: of that model's own, so the correction has become a way to go stale.
    redundant: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "priced": sorted(self.priced),
            "unpriced": sorted(self.unpriced),
            "rowsWritten": self.rows_written,
            "rowsKeptManual": self.rows_kept_manual,
            "overridden": sorted(self.overridden),
            "redundant": sorted(self.redundant),
        }


def models_in_use(conn: sqlite3.Connection) -> list[str]:
    """Every model string that could need a price.

    Includes models named on events that carry no tokens themselves: Codex
    records usage on events that name no model and the model is carried
    forward from its thread (see `cost.py`), so a model that never appears on
    a token-bearing event is still doing pricing work.
    """
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT model FROM event WHERE model IS NOT NULL ORDER BY model"
        )
    ]


def sync(conn: sqlite3.Connection, models: Sequence[str] | None = None) -> SyncResult:
    """Write catalog rates into `model_price` for every model in use.

    Manual rows are never touched -- not the row, not its siblings at other
    dates. Catalog rows for a model are replaced wholesale, so a rate that
    upstream corrects or withdraws does not linger.
    """
    names = list(models) if models is not None else models_in_use(conn)
    result = SyncResult(priced=[], unpriced=[], rows_written=0, rows_kept_manual=0)
    now = db.now_ms()

    for model in names:
        rates = rates_from_catalog(model)
        manual = {
            r[0] for r in conn.execute(
                "SELECT effective_from FROM model_price WHERE model = ? AND origin = ?",
                (model, MANUAL),
            )
        }
        result.rows_kept_manual += len(manual)
        if not rates:
            if not manual:
                result.unpriced.append(model)
            else:
                result.priced.append(model)
            continue

        if rates[0].origin == OVERRIDE:
            result.overridden.append(model)
            # An override that upstream has caught up with is no longer a
            # correction, only a way to go stale. Say so rather than letting
            # it sit there outranking a maintained entry forever.
            upstream = resolve(model)
            if upstream is not None and upstream["id"] == model:
                result.redundant.append(model)

        conn.execute(
            "DELETE FROM model_price WHERE model = ? AND origin <> ?", (model, MANUAL)
        )
        wrote = 0
        for r in rates:
            if r.effective_from in manual:
                continue  # a human priced this date; leave it alone
            conn.execute(
                """INSERT INTO model_price
                     (model, effective_from, input_mtok, output_mtok, cache_read_mtok,
                      cache_write_mtok, currency, origin, matched_id, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (model, r.effective_from, r.input_mtok, r.output_mtok, r.cache_read_mtok,
                 r.cache_write_mtok, r.currency, r.origin, r.matched_id, r.note, now),
            )
            wrote += 1
        result.rows_written += wrote
        result.priced.append(model)

    return result


def set_price(
    conn: sqlite3.Connection,
    model: str,
    *,
    effective_from: int = 0,
    input_mtok: float | None = None,
    output_mtok: float | None = None,
    cache_read_mtok: float | None = None,
    cache_write_mtok: float | None = None,
    currency: str = "USD",
    note: str | None = None,
) -> None:
    """Write one manual rate. `sync()` will not overwrite it."""
    conn.execute(
        """INSERT INTO model_price
             (model, effective_from, input_mtok, output_mtok, cache_read_mtok,
              cache_write_mtok, currency, origin, matched_id, note, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
           ON CONFLICT (model, effective_from) DO UPDATE SET
             input_mtok = excluded.input_mtok,
             output_mtok = excluded.output_mtok,
             cache_read_mtok = excluded.cache_read_mtok,
             cache_write_mtok = excluded.cache_write_mtok,
             currency = excluded.currency,
             origin = excluded.origin,
             matched_id = NULL,
             note = excluded.note,
             updated_at = excluded.updated_at""",
        (model, effective_from, input_mtok, output_mtok, cache_read_mtok,
         cache_write_mtok, currency, MANUAL, note, db.now_ms()),
    )


def clear_price(conn: sqlite3.Connection, model: str, effective_from: int | None = None) -> int:
    """Drop manual rows for a model. Returns how many went."""
    if effective_from is None:
        cur = conn.execute(
            "DELETE FROM model_price WHERE model = ? AND origin = ?", (model, MANUAL)
        )
    else:
        cur = conn.execute(
            "DELETE FROM model_price WHERE model = ? AND effective_from = ? AND origin = ?",
            (model, effective_from, MANUAL),
        )
    return cur.rowcount or 0


def load_rates(conn: sqlite3.Connection) -> dict[str, list[Rates]]:
    """Every stored rate, by model, oldest first -- the input to `cost.py`.

    A manual row and a catalog row can never collide: they share a primary
    key, so the table itself enforces one rate per (model, date).
    """
    out: dict[str, list[Rates]] = {}
    for row in conn.execute(
        """SELECT model, effective_from, input_mtok, output_mtok, cache_read_mtok,
                  cache_write_mtok, currency, origin, matched_id, note
           FROM model_price ORDER BY model, effective_from"""
    ):
        out.setdefault(row["model"], []).append(Rates(
            effective_from=row["effective_from"],
            input_mtok=row["input_mtok"],
            output_mtok=row["output_mtok"],
            cache_read_mtok=row["cache_read_mtok"],
            cache_write_mtok=row["cache_write_mtok"],
            currency=row["currency"],
            origin=row["origin"],
            matched_id=row["matched_id"],
            note=row["note"],
        ))
    return out


def approximations(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Models priced as a *neighbour*, as (model, the catalog id used).

    The catalog matches by pattern, so a model it has not been updated for is
    silently priced as its closest relative: `claude-fable-5-1` is priced as
    `claude-fable-5`, whose cache reads cost four times as much, and on a
    corpus with billions of cache-read tokens that single substitution moves
    the total by thousands. The substitution is defensible as a default -- a
    close relative beats no number -- but it must never be invisible, so every
    surface that prints a total prints this list too, and `cci price set`
    is the fix.
    """
    return [
        (row["model"], row["matched_id"])
        for row in conn.execute(
            """SELECT DISTINCT model, matched_id FROM model_price
               WHERE origin = ? AND matched_id IS NOT NULL
               ORDER BY model""",
            (GENAI,),
        )
        if row["matched_id"].split("/", 1)[-1] != row["model"]
    ]


def currency_in_use(conn: sqlite3.Connection) -> str:
    """The currency of the rates that actually priced something, or "mixed".

    Joined on `(model, price_from)` -- the rate each event was priced at --
    not merely on the model. Asking "which currencies does any rate for these
    models use" reports "mixed" for a table holding a USD rate and a EUR one
    dated next year that has never priced a single event, which is exactly
    the kind of alarm that teaches people to ignore alarms.

    Defined once here because three callers had their own copy of the wrong
    version and could disagree with each other about the same database.
    """
    rows = [r[0] for r in conn.execute(
        """SELECT DISTINCT p.currency
           FROM event_cost c
           JOIN model_price p ON p.model = c.model AND p.effective_from = c.price_from"""
    )]
    return rows[0] if len(rows) == 1 else ("mixed" if rows else "USD")


def rates_at(rates: Iterable[Rates], ts: int) -> Rates | None:
    """The newest rate not later than `ts`.

    A model whose earliest rate starts *after* an event is unpriced for that
    event rather than priced at a rate that did not exist yet.
    """
    best: Rates | None = None
    for r in rates:
        if r.effective_from <= ts and (best is None or r.effective_from > best.effective_from):
            best = r
    return best


__all__ = [
    "CATALOG_PATH",
    "COMPONENTS",
    "GENAI",
    "MANUAL",
    "OVERRIDE",
    "OVERRIDES_PATH",
    "Rates",
    "SyncResult",
    "approximations",
    "catalog",
    "catalog_source",
    "override_catalog",
    "clear_price",
    "currency_in_use",
    "day_to_ms",
    "load_rates",
    "models_in_use",
    "rates_at",
    "rates_from_catalog",
    "resolve",
    "resolve_override",
    "set_price",
    "sync",
]
