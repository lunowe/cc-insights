#!/usr/bin/env python3
"""Rebuild `src/cc_insights/model_prices.json` from LiteLLM, with models.dev
as the fallback.

Run this, not `cci price sync` -- that command reads the snapshot this script
writes. The split is deliberate: the network call happens once, in a checkout,
by a human who can read the diff; every machine afterwards prices offline from
a committed file. A tool whose pitch is that your logs never leave the laptop
should not reach out to the internet on a schedule to do arithmetic.

**Sources.**

* LiteLLM's `model_prices_and_context_window.json` (MIT,
  https://github.com/BerriAI/litellm) -- the catalog ccusage prices from, and
  the one that carries new Anthropic and OpenAI models first. The commit that
  last touched the file is resolved first and the file is fetched *at that
  commit*, so the recorded revision is the content, not merely near it.
* models.dev's `api.json` (MIT, https://models.dev), only for a model LiteLLM
  has no key for. The served file has no revision of its own, so its sha256
  is recorded instead. Among the catalogs that list a model, the strongest
  claim wins by ccusage's trust rule (spec section 1.3): the model's own
  vendor, then a cloud platform, then a reseller; ties go to the claim with a
  long-context tier, a cache-read rate, a cache-write rate, a context limit,
  then the alphabetically first catalog.

**What is kept.** Not all 4,000-odd LiteLLM keys: a fuzzy match can only
reach a key the snapshot holds, and a reseller or regional key (`azure/...`,
`openrouter/...:batch`) is exactly what a bare name must never be priced as.

1. Anthropic and OpenAI first-party keys: bare `claude-*` and `gpt-*`, and
   `anthropic/*` and `openai/*`, whose LiteLLM provider is the vendor itself,
   for text models (chat, responses, completion).
2. What opencode users hit. opencode's default catalog is its Zen gateway
   (models.dev provider `opencode`), and opencode logs a bare model id, so
   every Zen id -- plus every id in the Anthropic and OpenAI catalogs
   models.dev lists -- is priced: from LiteLLM's bare key or the model
   vendor's own LiteLLM prefix (`xai/grok-4.7`) if there is one, otherwise
   from models.dev. The ids seen in local opencode and Codex logs on the
   machine this was written on (`gpt-*`, two Zen free tiers) are all inside
   that set.

Free tiers (`*-free`, `*-contributor-free`) are never added: they cost
nothing, and an entry for one is a key a paid name could fuzzy-match.

**What each rate is.** LiteLLM publishes per-token rates; the snapshot stores
per million tokens, the shape vendors publish and `model_price` keeps.

* input, output, cache read, and the five-minute cache write map from
  `input_cost_per_token`, `output_cost_per_token`,
  `cache_read_input_token_cost` and `cache_creation_input_token_cost`.
* The one-hour cache write is `cache_creation_input_token_cost_above_1hr`
  when published. ccusage ignores that field and hard-codes 2x input; the
  published number is kept here, and for every Anthropic model it equals 2x.
  For an Anthropic model without the field the vendor's published rule (2x
  base input) is applied; no other vendor gets a guessed one-hour rate.
* A rate that is not published is None -- unpriced, not free. ccusage
  derives a cache read of 0.1x input and a cache write of 1.25x input where
  none is listed; that is a guess, and `cost.py` reports such tokens as
  unpriced instead.
* Long-context tiers (`*_above_200k_tokens`, `*_above_272k_tokens`, models.dev
  `tiers`) are flattened to the base rate and the clause marked `tiered`, so
  every row priced from it says so. Per-request tier pricing is not done.
* Batch, flex, priority and fast rates are not read: the logs do not say
  which a request used.

**Dates.** LiteLLM is not dated, so the history is kept here. When a key's
rates differ from the committed snapshot's, the old clause stays and the new
rate is appended dated the day it was fetched -- the change happened at some
point since the previous fetch, which is the honest resolution available.
That is right for a vendor's price change and wrong for upstream fixing a
typo, so every change is printed for a human to judge, and `--rebase KEY`
replaces a key's history with today's rate instead.

Usage:
    python3 scripts/sync_prices.py              # fetch, rewrite the snapshot
    python3 scripts/sync_prices.py --check      # exit 1 if the snapshot is stale
    python3 scripts/sync_prices.py --rebase gpt-5.6-sol   # a correction, not a change
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cc_insights.pricing import (  # noqa: E402 - after the path tweak
    COMPONENTS,
    LITELLM,
    MODELS_DEV,
    canonical,
    is_free_tier,
    lookup,
    strip_date,
)

OUT = ROOT / "src" / "cc_insights" / "model_prices.json"

LITELLM_REPO = "BerriAI/litellm"
LITELLM_FILE = "model_prices_and_context_window.json"
LITELLM_COMMIT_URL = (f"https://api.github.com/repos/{LITELLM_REPO}/commits"
                      f"?path={LITELLM_FILE}&sha=main&per_page=1")
LITELLM_RAW = f"https://raw.githubusercontent.com/{LITELLM_REPO}/{{rev}}/{LITELLM_FILE}"
MODELS_DEV_URL = "https://models.dev/api.json"

#: LiteLLM keys taken whole: bare (`claude-*`, `gpt-*`, and OpenAI's `o3`,
#: `codex-mini-latest`, which Codex has run) or `anthropic/` / `openai/`...
FIRST_PARTY_PREFIXES = ("anthropic/", "openai/")
#: ...when LiteLLM's provider for the key is the vendor itself.
FIRST_PARTY_PROVIDERS = {"anthropic", "openai", "text-completion-openai"}
#: LiteLLM drops a Claude model from its first-party keys once Anthropic
#: retires it, but keeps it under Bedrock's and Vertex's ids, at Anthropic's
#: list price. Claude Code logs from those months name the bare model, so
#: those keys price it: `anthropic.claude-3-7-sonnet-20250219-v1:0` and
#: `vertex_ai/claude-3-haiku@20240307`. Regional Bedrock ids (`us.`, `eu.`)
#: carry a premium and are not read.
RETIRED_CLAUDE = re.compile(
    r"^(?:anthropic\.(claude-[a-z0-9.-]+?)-v\d+:\d+|vertex_ai/(claude-[a-z0-9.-]+?)(?:@(\d{8}))?)$")
#: Text models. Image, audio, video, embedding and realtime keys price other
#: units, and none of the logs this reads record them.
TEXT_MODES = {"chat", "responses", "completion", None}
#: LiteLLM's prefix for a model's own vendor, tried for an id opencode users
#: hit that LiteLLM keys only under that prefix (`xai/grok-4.7`). Resellers
#: (`openrouter/`, `azure_ai/`, `novita/`, ...) are deliberately absent.
VENDOR_PREFIXES = ("xai", "gemini", "deepseek", "zai", "moonshot", "meta",
                   "mistral", "dashscope", "minimax")
#: models.dev catalogs whose every model is wanted: opencode's Zen gateway,
#: and the two first-party vendors.
MODELS_DEV_WANTED = ("opencode", "anthropic", "openai")

#: ccusage's trust ranking, `core/models-dev-catalog-rules.json`.
MODELS_DEV_OWNERS = {
    "ai21", "aisingapore", "alibaba", "amazon", "anthropic", "arcee-ai",
    "bytedance-seed", "cohere", "deepreinforce", "deepseek", "google", "ibm",
    "inclusionai", "liquid", "meituan", "meta", "microsoft", "minimax",
    "mistral", "mixedbread", "moonshotai", "motif-technologies", "nex-agi",
    "nvidia", "openai", "openbmb", "perplexity", "poolside", "quiverai",
    "sakana", "sarvam", "sdaia", "stepfun", "swiss-ai", "tencent",
    "thinkingmachines", "trendyol", "typesafe", "unbiased", "upstage",
    "vispark", "vivgrid", "writer", "xai", "xiaomi", "zai", "zhipuai",
}
MODELS_DEV_PLATFORMS = {"amazon-bedrock", "azure", "azure-cognitive-services",
                        "google-vertex", "google-vertex-anthropic"}
TRUST = {3: "owner", 2: "platform", 1: "reseller"}
TRUST_RANK = {name: rank for rank, name in TRUST.items()}

#: Anthropic's published rule, base input -> one-hour cache write.
#: https://platform.claude.com/docs/en/about-claude/pricing
CACHE_WRITE_1H_MULTIPLIER = 2.0

_LITELLM_TIER = re.compile(
    r"^(input_cost_per_token|output_cost_per_token|cache_read_input_token_cost"
    r"|cache_creation_input_token_cost)_above_\d+k_tokens$")

#: A rate change beyond this is printed for a human to check.
REPORT_THRESHOLD = 0.01


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def fetch_bytes(url: str, accept: str = "application/json") -> bytes:
    """GET a URL, falling back to curl.

    A python.org install ships no CA bundle until someone runs
    `Install Certificates.command`, and this script failing with an SSL error
    on a fresh checkout would read as "the catalog is unreachable". curl uses
    the system trust store and is on every machine this runs on.
    """
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "cc-insights"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https URL
            return resp.read()
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLError):
            raise
        out = subprocess.run(["curl", "-fsSL", "-H", f"Accept: {accept}", url],
                             capture_output=True, check=True)
        return out.stdout


def fetch_sources() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(litellm, models.dev, provenance), each catalog pinned as tightly as it allows."""
    try:
        commits = json.loads(fetch_bytes(LITELLM_COMMIT_URL, "application/vnd.github+json"))
        rev = commits[0]["sha"]
    except Exception as exc:  # provenance is worth a warning, not a failure
        print(f"warning: could not resolve LiteLLM's commit ({exc}); using main",
              file=sys.stderr)
        rev = None
    litellm_url = LITELLM_RAW.format(rev=rev or "main")
    litellm = json.loads(fetch_bytes(litellm_url))
    md_bytes = fetch_bytes(MODELS_DEV_URL)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    source = {
        "repo": LITELLM_REPO,
        "url": litellm_url,
        "commit": rev,
        "license": "MIT",
        "fetched_at": fetched_at,
        "fallback": {
            "name": "models.dev",
            "url": MODELS_DEV_URL,
            "sha256": hashlib.sha256(md_bytes).hexdigest(),
            "license": "MIT",
            "fetched_at": fetched_at,
        },
    }
    return litellm, json.loads(md_bytes), source


# --------------------------------------------------------------------------
# turning upstream entries into clauses
# --------------------------------------------------------------------------


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def per_mtok(value: Any, scale: float = 1_000_000) -> float | None:
    """Per token -> per million, without float noise (4e-06 -> 4.0, not 3.9999...)."""
    if not _number(value):
        return None
    return float(f"{value * scale:.10g}")


def litellm_clause(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """One LiteLLM entry as a clause, or None when it is not a priced text model."""
    if entry.get("mode") not in TEXT_MODES:
        return None
    inp, out = entry.get("input_cost_per_token"), entry.get("output_cost_per_token")
    if not _number(inp) or not _number(out) or (inp == 0 and out == 0):
        return None
    clause: dict[str, Any] = {
        "start_date": None,
        "input_mtok": per_mtok(inp),
        "output_mtok": per_mtok(out),
        "cache_read_mtok": per_mtok(entry.get("cache_read_input_token_cost")),
        "cache_write_mtok": per_mtok(entry.get("cache_creation_input_token_cost")),
        "cache_write_1h_mtok": per_mtok(entry.get("cache_creation_input_token_cost_above_1hr")),
    }
    if clause["cache_write_1h_mtok"] is None and entry.get("litellm_provider") == "anthropic":
        clause["cache_write_1h_mtok"] = per_mtok(inp * CACHE_WRITE_1H_MULTIPLIER)
    if any(_LITELLM_TIER.match(k) for k in entry):
        clause["tiered"] = True
    return clause


def models_dev_clause(model: Mapping[str, Any], provider: str,
                      claude: bool = False) -> dict[str, Any] | None:
    """One models.dev model as a clause (its rates are already per MTok)."""
    cost = model.get("cost")
    if not isinstance(cost, dict):
        return None
    inp, out = cost.get("input"), cost.get("output")
    if not _number(inp) or not _number(out) or (inp == 0 and out == 0):
        return None
    outputs = (model.get("modalities") or {}).get("output")
    if isinstance(outputs, list) and "text" not in outputs:
        return None
    clause: dict[str, Any] = {
        "start_date": None,
        "input_mtok": per_mtok(inp, 1),
        "output_mtok": per_mtok(out, 1),
        "cache_read_mtok": per_mtok(cost.get("cache_read"), 1),
        "cache_write_mtok": per_mtok(cost.get("cache_write"), 1),
        "cache_write_1h_mtok": None,
    }
    # Anthropic, and the clouds that resell Claude at Anthropic's list
    # price, publish the 2x rule; nobody else gets a guessed one-hour rate.
    if provider == "anthropic" or (provider in MODELS_DEV_PLATFORMS and claude):
        clause["cache_write_1h_mtok"] = per_mtok(inp * CACHE_WRITE_1H_MULTIPLIER, 1)
    if cost.get("tiers") or cost.get("context_over_200k"):
        clause["tiered"] = True
    return clause


def trust_of(provider: str) -> int:
    if provider in MODELS_DEV_OWNERS:
        return 3
    if provider in MODELS_DEV_PLATFORMS:
        return 2
    return 1


def models_dev_claims(models_dev: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The strongest models.dev claim per model id, keyed by canonical id."""
    best: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
    for provider in sorted(models_dev):
        catalog = models_dev[provider]
        if not isinstance(catalog, dict):
            continue
        for key in sorted(catalog.get("models") or {}):
            model = catalog["models"][key]
            if not isinstance(model, dict) or is_free_tier(key):
                continue
            clause = models_dev_clause(
                model, provider, key.rsplit("/", 1)[-1].lower().startswith("claude-"))
            if clause is None:
                continue
            cost, trust = model["cost"], trust_of(provider)
            strength = (trust, bool(cost.get("tiers")), cost.get("cache_read") is not None,
                        cost.get("cache_write") is not None,
                        bool((model.get("limit") or {}).get("context")))
            cid = canonical(key)
            # Strictly stronger replaces; on a tie the first seen (sorted by
            # catalog, then key) stays.
            if cid not in best or strength > best[cid][0]:
                best[cid] = (strength, {
                    "source": MODELS_DEV, "upstream": f"{provider}/{key}",
                    "provider": provider, "trust": TRUST[trust], "clauses": [clause],
                })
    return {cid: entry for cid, (_, entry) in best.items()}


def wanted_ids(models_dev: Mapping[str, Any]) -> dict[str, int]:
    """Model id -> the least trust a models.dev claim on it needs.

    Every id in the catalogs of `MODELS_DEV_WANTED` takes any claim: for a
    Zen id, the gateway's own price is what an opencode user pays. Beyond
    those, every `claude-*` / `gpt-*` id any catalog lists is wanted too --
    LiteLLM drops a model once it is retired, and history still ran on it --
    but only from the vendor or a cloud platform reselling at list price, so
    a reseller's markup or its own invented tier never prices a vendor model.
    One id per spelling (`claude-3.5-haiku` = `claude-3-5-haiku`).
    """
    wanted: dict[str, int] = {}
    seen: dict[str, str] = {}

    def want(model_id: str, trust: int) -> None:
        # `:` and `@` are a platform's addressing (`...-v1:0`, `...@default`),
        # not a model name anything logs.
        if is_free_tier(model_id) or ":" in model_id or "@" in model_id:
            return
        # Keyed without its date, so the dated and the bare name both find it.
        cid = canonical(model_id)
        key = seen.setdefault(cid, strip_date(model_id))
        wanted[key] = min(wanted.get(key, trust), trust)

    for provider in MODELS_DEV_WANTED:
        for key in ((models_dev.get(provider) or {}).get("models") or {}):
            want(key.rsplit("/", 1)[-1], 1)
    for provider in sorted(models_dev):
        catalog = models_dev[provider]
        for key in ((catalog if isinstance(catalog, dict) else {}).get("models") or {}):
            bare = key.rsplit("/", 1)[-1].lower()
            if bare.startswith(("claude-", "gpt-")):
                want(bare, 2)
    return dict(sorted(wanted.items()))


def build(litellm: Mapping[str, Any], models_dev: Mapping[str, Any],
          source: dict[str, Any]) -> dict[str, Any]:
    """The snapshot, without history (see `carry_history`)."""
    models: dict[str, dict[str, Any]] = {}

    for key in sorted(litellm):
        entry = litellm[key]
        if (not isinstance(entry, dict)
                or ("/" in key and not key.startswith(FIRST_PARTY_PREFIXES))
                or entry.get("litellm_provider") not in FIRST_PARTY_PROVIDERS
                or is_free_tier(key)):
            continue
        clause = litellm_clause(entry)
        if clause:
            models[key] = {"source": LITELLM, "upstream": key,
                           "provider": entry["litellm_provider"], "clauses": [clause]}

    retired: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for key in sorted(litellm):
        m = RETIRED_CLAUDE.match(key)
        entry = litellm[key]
        if not m or not isinstance(entry, dict):
            continue
        model_id = m.group(1) or (m.group(2) + (f"-{m.group(3)}" if m.group(3) else ""))
        if lookup(models, model_id, fuzzy=False):
            continue  # the vendor's own key prices it
        clause = litellm_clause({**entry, "litellm_provider": "anthropic"})
        if clause:
            retired.setdefault(strip_date(model_id), []).append((model_id, key, clause))
    for bare, found in retired.items():
        # Bedrock's keys carry cache rates and Vertex's mostly do not, so
        # Vertex prices a model only Bedrock lacks. Then one entry under the
        # undated name when every dated key agrees on the price; otherwise
        # one per dated id, which a dated log line still finds exactly.
        bedrock = [f for f in found if f[1].startswith("anthropic.")]
        found = bedrock or found
        agree = len({rates(c) for _, _, c in found}) == 1
        for model_id, key, clause in found:
            models.setdefault(bare if agree else model_id, {
                "source": LITELLM, "upstream": key, "provider": "anthropic",
                "trust": "platform", "clauses": [clause]})

    claims = models_dev_claims(models_dev)
    for model_id, min_trust in wanted_ids(models_dev).items():
        if lookup(models, model_id, fuzzy=False):
            continue  # already priced exactly
        # LiteLLM may key the model only by its dated snapshot
        # (`claude-sonnet-4-20250514`). A lookup never reaches a longer key
        # from a shorter name, so give the bare id an entry of its own -- but
        # only when every dated key agrees, or the choice would be a guess.
        same = [k for k in models if canonical(k) == canonical(model_id)]
        if same and len({rates(models[k]["clauses"][-1]) for k in same}) == 1:
            pick = models[max(same)]
            models[model_id] = {**pick, "clauses": [dict(c) for c in pick["clauses"]]}
            continue
        candidates = [model_id] + [f"{p}/{model_id}" for p in VENDOR_PREFIXES]
        upstream = next((c for c in candidates if isinstance(litellm.get(c), dict)), None)
        if upstream is not None:
            if litellm[upstream].get("mode") not in TEXT_MODES:
                continue  # LiteLLM knows it, and it is not a text model
            clause = litellm_clause(litellm[upstream])
            if clause:
                models[model_id] = {"source": LITELLM, "upstream": upstream,
                                    "provider": litellm[upstream].get("litellm_provider"),
                                    "clauses": [clause]}
                continue
        claim = claims.get(canonical(model_id))
        if claim is not None and TRUST_RANK[claim["trust"]] >= min_trust:
            models[model_id] = claim

    return {"source": source, "models": dict(sorted(models.items()))}


# --------------------------------------------------------------------------
# history, and what changed
# --------------------------------------------------------------------------


def rates(clause: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(clause.get(f"{c}_mtok") for c in COMPONENTS)


def carry_history(built: dict[str, Any], previous: Mapping[str, Any] | None,
                  today: str, rebase: Iterable[str] = ()) -> None:
    """Keep the committed snapshot's dated clauses; date a changed rate today.

    A previous snapshot in another shape (the genai-prices one this replaced)
    carries no history over: its dates belong to a different catalog.
    """
    old_models = (previous or {}).get("models")
    if not isinstance(old_models, dict):
        return
    rebase = set(rebase)
    for key, entry in built["models"].items():
        old = old_models.get(key)
        if key in rebase or not isinstance(old, dict) or not old.get("clauses"):
            continue
        new_clause = entry["clauses"][-1]
        last = old["clauses"][-1]
        if rates(last) == rates(new_clause):
            entry["clauses"] = old["clauses"][:-1] + [{**new_clause,
                                                        "start_date": last.get("start_date")}]
        else:
            entry["clauses"] = old["clauses"] + [{**new_clause, "start_date": today}]


def changes(previous: Mapping[str, Any] | None, built: Mapping[str, Any],
            threshold: float = REPORT_THRESHOLD) -> list[tuple[str, tuple, tuple]]:
    """(key, old rates, new rates) for every key whose current rate moved."""
    old_models = (previous or {}).get("models")
    if not isinstance(old_models, dict):
        return []
    out = []
    for key, entry in built["models"].items():
        old = old_models.get(key)
        if not isinstance(old, dict) or not old.get("clauses"):
            continue
        a, b = rates(old["clauses"][-1]), rates(entry["clauses"][-1])
        if any(_moved(x, y, threshold) for x, y in zip(a, b)):
            out.append((key, a, b))
    return out


def _moved(old: float | None, new: float | None, threshold: float) -> bool:
    if old is None or new is None:
        return old is not new
    if old == 0:
        return new != 0
    return abs(new - old) / abs(old) > threshold


def comparable(snapshot: Mapping[str, Any]) -> str:
    """The current rate of every key, so `--check` tracks content, not fetch time."""
    models = snapshot.get("models")
    if not isinstance(models, dict):
        return ""
    return json.dumps({k: rates(v["clauses"][-1]) for k, v in models.items()}, sort_keys=True)


def _fmt(r: tuple) -> str:
    return "/".join("-" if x is None else f"{x:g}" for x in r)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed snapshot differs from upstream")
    ap.add_argument("--rebase", action="append", default=[], metavar="KEY",
                    help="replace KEY's history with today's rate (a correction, "
                         "not a price change); repeatable")
    args = ap.parse_args(argv)

    litellm, models_dev, source = fetch_sources()
    built = build(litellm, models_dev, source)
    previous = json.loads(OUT.read_text()) if OUT.exists() else None

    if args.check:
        if previous is None:
            print(f"{OUT} is missing", file=sys.stderr)
            return 1
        if comparable(previous) == comparable(built):
            print(f"{OUT.name} is up to date ({len(built['models'])} models)")
            return 0
        print(f"{OUT.name} is stale; re-run without --check", file=sys.stderr)
        return 1

    carry_history(built, previous, source["fetched_at"][:10], args.rebase)
    moved = changes(previous, built)
    OUT.write_text(json.dumps(built, indent=1) + "\n")

    by_source: dict[str, int] = {}
    for entry in built["models"].values():
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {len(built['models'])} models: "
          + ", ".join(f"{n} from {s}" for s, n in sorted(by_source.items())))
    print(f"  LiteLLM @ {source['commit'] or 'main'}, models.dev sha256 "
          f"{source['fallback']['sha256'][:12]}")
    if moved:
        print(f"\n  {len(moved)} rate(s) changed by more than {REPORT_THRESHOLD:.0%} "
              "(input/output/cache read/cache write 5m/1h per MTok).")
        print("  Check each against the vendor's page. A real price change keeps its")
        print("  history; for an upstream correction, re-run with --rebase KEY.")
        for key, a, b in moved:
            print(f"    {key:<34} {_fmt(a)}  ->  {_fmt(b)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
