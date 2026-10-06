"""The price table: what a model cost, on the day it ran.

This module owns `model_price` and nothing else. It does not touch events and
it does not compute money -- that is `cost.py`. The split matters because the
two change for different reasons: prices change when a vendor publishes,
costs change when logs arrive.

**A rate is a property of a model on a day, not of a model.** Pricing
August's traffic at September's rate would quietly rewrite last month's total
every time a vendor moves a number, which is exactly the kind of drift that
makes a dashboard untrustworthy without ever looking broken. So rows are keyed
(model, effective_from) and an event is priced with the newest row not later
than the event. LiteLLM publishes no dates, so `scripts/sync_prices.py` keeps
the history itself: a rate that changes between two snapshots gets a new
clause dated the day the change was first seen, and the old one stays.

**Where the numbers come from.** `model_prices.json`, a snapshot committed to
this repo and refreshed by `scripts/sync_prices.py`: LiteLLM's
`model_prices_and_context_window.json` -- the catalog ccusage prices from --
for Anthropic and OpenAI first-party keys and the models opencode users hit,
with models.dev as the fallback for a model LiteLLM lacks. Nothing here
reaches the network: pricing is arithmetic over a local file, and a tool that
promises your logs stay on the laptop should not phone out to do arithmetic.

**Matching is by the model string the log recorded, the way ccusage does it.**
An exact key wins: the string itself, then without a provider prefix
(`anthropic/`, `openai/`), then without an 8-digit date suffix, then the same
with `.` and `@` spelled `-`. Only then a fuzzy match -- a catalog key
occurring inside the string at word boundaries, longest key first -- and only
under ccusage's version rule: a key ending in a digit never matches when what
follows it is `-<digits>` or `.<digits>`. That rule is the point. A prefix
matcher let `claude-opus-5-5` resolve to `claude-opus-5` at $5/$25 when it
costs $4/$20; under this rule it can only ever match its own key or nothing,
and `gpt-6.1-sol` can never be priced as `gpt-6-sol`. A fuzzy match is
recorded, and `approximations()` names it beside every total.

Free tiers (`*-free`, `*-contributor-free`, `:free`) are never fuzzy-matched:
a free model priced as its paid sibling invents a bill that was never sent.
They stay unpriced unless a human sets a rate.

**Three layers, and the more specific one wins.**

1. `model_prices.json` -- the catalog snapshot. Broad and maintained
   upstream, and still sometimes wrong about a model it has just added.
2. `price_overrides.json` -- corrections checked against the vendor's own
   pricing page, shipped with the code. This layer exists because a rate
   correction that lives in one laptop's database is lost on the next machine
   and on the next rebuild, and because catalog errors are not small: the
   previous catalog priced `claude-fable-5-1` as `claude-fable-5`, whose
   cache reads cost four times as much, which on the measured corpus was 21%
   of the total. An override matches exact spellings only -- never fuzzily --
   so it cannot reach a model it does not name.
3. `origin = 'manual'` -- `cci price set`. A human beats both and `sync()`
   never touches it, the same rule `project_group.origin` follows. That is
   the escape hatch for what neither file can know: an enterprise discount,
   or a model that exists only inside one company.

An override is a full replacement, not a patch: reading one row tells you all
five rates a model was charged at, rather than sending you to a second file
to find out which of them came from where. The cost is that an override
freezes the rates it does not correct, so each one records `checked` and
`source`, and `sync()` says when upstream agrees and the override can go.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cc_insights import db

#: The committed catalog snapshot. Rebuilt by `scripts/sync_prices.py`.
CATALOG_PATH = Path(__file__).resolve().parent / "model_prices.json"

#: Hand-checked corrections to it. Edited by a human, never by a script.
OVERRIDES_PATH = Path(__file__).resolve().parent / "price_overrides.json"

#: Catalog origins: which upstream file a snapshot entry came from.
LITELLM = "litellm"
MODELS_DEV = "models.dev"
#: Shipped correction to the catalog. Beats it; loses to MANUAL.
OVERRIDE = "override"
MANUAL = "manual"

#: Rate columns, in the order every report prints them. `cache_write` is the
#: five-minute rate and `cache_write_1h` the one-hour one -- two prices for
#: the same tokens, 1.25x and 2x base input, and on this corpus 41% of cache
#: writes take the expensive one. See migration 005.
COMPONENTS = ("input", "output", "cache_read", "cache_write", "cache_write_1h")

_TIERED_NOTE = (
    "long-context tiers flattened to the base rate: a request past the "
    "threshold is priced as if it were not, so this under-states such requests"
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
    #: Five-minute cache writes.
    cache_write_mtok: float | None = None
    #: One-hour cache writes. NULL is "not known", never "same as 5m".
    cache_write_1h_mtok: float | None = None
    currency: str = "USD"
    origin: str = LITELLM
    matched_id: str | None = None
    note: str | None = None

    def rate(self, component: str) -> float | None:
        return getattr(self, f"{component}_mtok")


# --------------------------------------------------------------------------
# dates
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


# --------------------------------------------------------------------------
# matching a model string to a catalog key
# --------------------------------------------------------------------------

#: `-YYYYMMDD` (Anthropic) or `-YYYY-MM-DD` (OpenAI) at the end of a name.
_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
#: `x-free`, `x-contributor-free`, `vendor/x:free`.
_FREE_TIER = re.compile(r"(?:^|[-:])free$")


def _spell(value: str) -> str:
    """`.` and `@` read as `-`: `claude-fable-5.1` names `claude-fable-5-1`."""
    return value.replace(".", "-").replace("@", "-")


def strip_date(model: str) -> str:
    return _DATE_SUFFIX.sub("", model)


def is_free_tier(model: str) -> bool:
    """A name a vendor gives its free tier. Never priced as the paid model."""
    return bool(_FREE_TIER.search(model.strip().lower()))


def canonical(model: str) -> str:
    """The bare model a string names: no provider, no date, `-` separators,
    no Bedrock wrapper.

    Two strings with the same canonical form are the same model at the same
    price, which is how `approximations()` tells an exact match from a
    near relative after the fact, from nothing but the stored `matched_id`.
    """
    bare = model.strip().lower().rsplit("/", 1)[-1]
    bare = _PLATFORM_WRAPPER.sub("", bare)
    return strip_date(_spell(bare))


#: Bedrock's spelling of a model id: `us.anthropic.claude-x-20250514-v1:0`.
_PLATFORM_WRAPPER = re.compile(r"^(?:[a-z]+\.)+(?=[a-z])|-v\d+:\d+$")


def exact_spellings(model: str) -> list[str]:
    """Every string that names the same model, most literal first.

    The string as recorded; lower-cased; each provider prefix peeled off
    (`openrouter/anthropic/x` -> `anthropic/x` -> `x`); and each of those
    without a date suffix, including Vertex's `@YYYYMMDD` spelling of one.
    """
    raw = model.strip()
    low = raw.lower()
    parts = low.split("/")
    forms = [raw, low] + ["/".join(parts[i:]) for i in range(1, len(parts))]
    forms += [strip_date(f) for f in forms] + [strip_date(_spell(f)) for f in forms]
    return list(dict.fromkeys(f for f in forms if f))


def key_occurs_in(value: str, key: str) -> bool:
    """ccusage's fuzzy rule (`contains_pricing_key`, pricing.rs:2566-2610).

    `key` must sit inside `value` at a boundary: the character before it is
    the start or not alphanumeric, the one after is the end or not
    alphanumeric. And when the key ends in a digit, what follows must not be
    a further version number -- `-5` or `.1` -- unless it is an 8-digit date.
    So `claude-opus-5` matches `claude-opus-5-thinking` and
    `claude-opus-5-20260101`, never `claude-opus-5-5`.

    One rule stricter than ccusage: a key never reaches a name that adds a
    size or speed tier (`-mini`, `-pro`, `-fast`, ...; see `_TIER_WORDS`).
    Those are separately priced models -- `o1-mini` costs a thirteenth of
    `o1`, `claude-opus-5-fast` twice `claude-opus-5` -- so the near relative
    is not near, and unpriced is the more honest answer.
    """
    if not key:
        return False
    start = value.find(key)
    while start != -1:
        before_ok = start == 0 or not value[start - 1].isalnum()
        suffix = value[start + len(key):]
        if before_ok and _suffix_allows(key, suffix):
            return True
        start = value.find(key, start + 1)
    return False


#: Words that, following a model name, name a different model at a
#: different price. See `key_occurs_in`.
_TIER_WORDS = frozenset({
    "mini", "nano", "pro", "max", "lite", "flash", "fast", "turbo", "plus", "ultra",
})


def _suffix_allows(key: str, suffix: str) -> bool:
    if not suffix:
        return True
    if suffix[0].isalnum():
        return False
    if re.split(r"[^a-z0-9]", suffix[1:].lower(), maxsplit=1)[0] in _TIER_WORDS:
        return False
    if not key[-1].isdigit() or suffix[0] not in "-.":
        return True
    digits = len(suffix[1:]) - len(suffix[1:].lstrip("0123456789"))
    if digits == 0:
        return True
    after = suffix[1 + digits:2 + digits]
    return digits == 8 and (not after or not after.isalnum())


def _spelling_index(models: Mapping[str, Any]) -> dict[str, str | None]:
    """Separator-normalized key -> key. Two keys with one spelling name neither."""
    index: dict[str, str | None] = {}
    for key in models:
        spelled = _spell(key.lower())
        index[spelled] = None if spelled in index and index[spelled] != key else key
    return index


def lookup(
    models: Mapping[str, Any], model: str, *, fuzzy: bool = True
) -> tuple[str, bool] | None:
    """The key `model` is priced under, and whether that is an exact match.

    Exact first, in the order `exact_spellings` gives, then the same with
    `.`/`@` read as `-`. Then, if allowed and the name is not a free tier,
    the longest key that `key_occurs_in` the name -- LiteLLM keys before
    models.dev ones, as ccusage consults its primary map before the fallback,
    and on equal length the alphabetically first, so the answer is stable.

    The match is one-way: a key inside the model name. ccusage also accepts
    the name inside a longer key, which prices a bare `gpt-6` as whichever
    `gpt-6-*` variant is longest; that is a guess about which model ran, and
    an unpriced row says so more honestly.
    """
    spellings = exact_spellings(model)
    for s in spellings:
        if s in models:
            return s, True
    index = _spelling_index(models)
    for s in spellings:
        key = index.get(_spell(s))
        if key is not None:
            return key, True
    if not fuzzy or is_free_tier(model):
        return None

    value = model.strip().lower()
    spelled = _spell(value)
    best: tuple[int, int, str] | None = None
    for key, entry in models.items():
        low = key.lower()
        if is_free_tier(low):
            continue
        if key_occurs_in(value, low) or key_occurs_in(spelled, _spell(low)):
            source = entry.get("source") if isinstance(entry, dict) else None
            rank = (0 if source in (None, LITELLM) else 1, -len(key), key)
            if best is None or rank < best:
                best = rank
    return (best[2], False) if best else None


# --------------------------------------------------------------------------
# the catalog snapshot and the shipped corrections
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def catalog(path: str | None = None) -> dict[str, Any]:
    """The price snapshot, parsed once.

    A missing or unreadable snapshot is not fatal: every model is then
    unpriced and says so, which is a better failure than an exception out of
    `cci stats`. Neither is one in a shape this code does not know (the old
    genai-prices list): it reads as empty rather than as wrong prices.
    """
    target = Path(path) if path else CATALOG_PATH
    try:
        doc = json.loads(target.read_text())
    except (OSError, ValueError):
        return {"source": {}, "models": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict):
        return {"source": (doc or {}).get("source", {}) if isinstance(doc, dict) else {},
                "models": {}}
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
    """The catalog entry a model string is priced from, or None.

    The entry comes back with `key` (the snapshot key it matched) and `exact`
    (False for a fuzzy, near-relative match) added.
    """
    models = catalog().get("models", {})
    hit = lookup(models, model)
    if hit is None:
        return None
    key, exact = hit
    return {**models[key], "key": key, "exact": exact}


def resolve_override(model: str) -> dict[str, Any] | None:
    """The shipped correction for a model string, or None. Exact spellings only."""
    entries = {e["id"]: e for e in override_catalog().get("models", []) if e.get("id")}
    hit = lookup(entries, model, fuzzy=False)
    return entries[hit[0]] if hit else None


def _rate_tuple(clause: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(clause.get(f"{c}_mtok") for c in COMPONENTS)


def _clauses_to_rates(
    entry: dict[str, Any], origin: str, matched: str, note: str | None
) -> list[Rates]:
    out = []
    for clause in entry.get("clauses", []):
        notes = [n for n in (note, _TIERED_NOTE if clause.get("tiered") else None) if n]
        out.append(Rates(
            effective_from=day_to_ms(clause.get("start_date")),
            input_mtok=clause.get("input_mtok"),
            output_mtok=clause.get("output_mtok"),
            cache_read_mtok=clause.get("cache_read_mtok"),
            cache_write_mtok=clause.get("cache_write_mtok"),
            cache_write_1h_mtok=clause.get("cache_write_1h_mtok"),
            origin=origin,
            matched_id=matched,
            note="; ".join(notes) or None,
        ))
    out.sort(key=lambda r: r.effective_from)
    return out


def rates_from_catalog(model: str) -> list[Rates]:
    """Every dated rate known for a model, oldest first.

    A shipped override replaces the catalog outright for that model -- see the
    layering in the module docstring. The returned `Rates` carry the origin
    they came from, so the row written to `model_price` says which, and
    `matched_id` names the upstream entry (`litellm:claude-opus-5-5`,
    `models.dev:meta/muse-spark-1.3`) so a surprising rate can be traced.
    """
    entry = resolve_override(model)
    if entry is not None:
        return _clauses_to_rates(
            entry, OVERRIDE, f"{entry.get('provider', OVERRIDE)}/{entry['id']}", entry.get("why"))
    entry = resolve(model)
    if entry is None:
        return []
    source = entry.get("source", LITELLM)
    note = None
    if source == MODELS_DEV:
        note = (f"not in LiteLLM; priced from models.dev's {entry.get('provider')} "
                f"catalog ({entry.get('trust', 'unranked')})")
    return _clauses_to_rates(entry, source, f"{source}:{entry.get('upstream', entry['key'])}",
                             note)


def _override_is_redundant(model: str, override: dict[str, Any]) -> bool:
    """Upstream has an exact entry for the model and agrees on every rate."""
    upstream = resolve(model)
    if upstream is None or not upstream["exact"]:
        return False
    mine = [_rate_tuple(c) for c in override.get("clauses", [])]
    theirs = [_rate_tuple(c) for c in upstream.get("clauses", [])]
    return mine[-1:] == theirs[-1:] and bool(mine)


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
    #: Overrides upstream has caught up with -- the catalog now has an exact
    #: entry for the model at the same rates, so the correction has become a
    #: way to go stale.
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
            # Withdrawn upstream, or never really matched (a free tier the
            # old prefix matcher priced as its paid sibling): the stale row
            # must go, or it keeps pricing traffic it has no claim to.
            conn.execute(
                "DELETE FROM model_price WHERE model = ? AND origin <> ?", (model, MANUAL)
            )
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
            override = resolve_override(model)
            if override is not None and _override_is_redundant(model, override):
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
                      cache_write_mtok, cache_write_1h_mtok, currency, origin,
                      matched_id, note, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (model, r.effective_from, r.input_mtok, r.output_mtok, r.cache_read_mtok,
                 r.cache_write_mtok, r.cache_write_1h_mtok, r.currency, r.origin,
                 r.matched_id, r.note, now),
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
    cache_write_1h_mtok: float | None = None,
    currency: str = "USD",
    note: str | None = None,
) -> None:
    """Write one manual rate. `sync()` will not overwrite it."""
    conn.execute(
        """INSERT INTO model_price
             (model, effective_from, input_mtok, output_mtok, cache_read_mtok,
              cache_write_mtok, cache_write_1h_mtok, currency, origin, matched_id,
              note, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
           ON CONFLICT (model, effective_from) DO UPDATE SET
             input_mtok = excluded.input_mtok,
             output_mtok = excluded.output_mtok,
             cache_read_mtok = excluded.cache_read_mtok,
             cache_write_mtok = excluded.cache_write_mtok,
             cache_write_1h_mtok = excluded.cache_write_1h_mtok,
             currency = excluded.currency,
             origin = excluded.origin,
             matched_id = NULL,
             note = excluded.note,
             updated_at = excluded.updated_at""",
        (model, effective_from, input_mtok, output_mtok, cache_read_mtok,
         cache_write_mtok, cache_write_1h_mtok, currency, MANUAL, note, db.now_ms()),
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
                  cache_write_mtok, cache_write_1h_mtok, currency, origin,
                  matched_id, note
           FROM model_price ORDER BY model, effective_from"""
    ):
        out.setdefault(row["model"], []).append(Rates(
            effective_from=row["effective_from"],
            input_mtok=row["input_mtok"],
            output_mtok=row["output_mtok"],
            cache_read_mtok=row["cache_read_mtok"],
            cache_write_mtok=row["cache_write_mtok"],
            cache_write_1h_mtok=row["cache_write_1h_mtok"],
            currency=row["currency"],
            origin=row["origin"],
            matched_id=row["matched_id"],
            note=row["note"],
        ))
    return out


def approximations(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Models priced as a *neighbour*, as (model, the catalog id used).

    A fuzzy match prices a model the catalog has no key for as its closest
    relative: `claude-opus-5-thinking` as `claude-opus-5`. The boundary rule
    keeps that from crossing a version (`claude-opus-5-5` is never priced as
    `claude-opus-5`), but a sibling can still differ -- the previous catalog
    priced `claude-fable-5-1` as `claude-fable-5`, whose cache reads cost four
    times as much, and on a cache-read-heavy corpus that one substitution
    moved the total by thousands. The substitution is defensible as a
    default -- a close relative beats no number -- but it must never be
    invisible, so every surface that prints a total prints this list too,
    and `cci price set` is the fix.

    Read from the stored rows alone: a match is exact when the model and the
    matched id name the same bare model (`canonical`), so a provider prefix
    or a date suffix is not reported as a relative. Manual and override rows
    are never approximations; any other origin is a catalog's, including
    rows an older catalog wrote.
    """
    return [
        (row["model"], row["matched_id"])
        for row in conn.execute(
            """SELECT DISTINCT model, matched_id FROM model_price
               WHERE origin NOT IN (?, ?) AND matched_id IS NOT NULL
               ORDER BY model""",
            (MANUAL, OVERRIDE),
        )
        if canonical(_MATCHED_SOURCE.sub("", row["matched_id"])) != canonical(row["model"])
    ]


#: The `litellm:` / `models.dev:` that `rates_from_catalog` puts on a matched id.
_MATCHED_SOURCE = re.compile(r"^(?:litellm|models\.dev):")


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
    "LITELLM",
    "MANUAL",
    "MODELS_DEV",
    "OVERRIDE",
    "OVERRIDES_PATH",
    "Rates",
    "SyncResult",
    "approximations",
    "canonical",
    "catalog",
    "catalog_source",
    "clear_price",
    "currency_in_use",
    "day_to_ms",
    "exact_spellings",
    "is_free_tier",
    "key_occurs_in",
    "load_rates",
    "lookup",
    "models_in_use",
    "override_catalog",
    "rates_at",
    "rates_from_catalog",
    "resolve",
    "resolve_override",
    "set_price",
    "strip_date",
    "sync",
]
