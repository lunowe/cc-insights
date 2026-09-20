#!/usr/bin/env python3
"""Rebuild `src/cc_insights/model_prices.json` from the genai-prices catalog.

Run this, not `cci price sync` -- that command reads the snapshot this script
writes. The split is deliberate: the network call happens once, in a checkout,
by a human who can read the diff; every machine afterwards prices offline from
a committed file. A tool whose pitch is that your logs never leave the laptop
should not reach out to the internet on a schedule to do arithmetic.

Source: https://github.com/pydantic/genai-prices (MIT), `prices/data.json`.
Why that catalog rather than a table typed out here: it is dated (it knows
Sonnet 5 changed price on 2026-09-01), it is maintained against vendor pricing
pages by people who do that on purpose, and it is auditable -- every row this
writes records the provider and model id it came from, so a surprising rate
leads back to a file a human can go read.

What is thrown away, and why it is safe:

* **Everything but the four token rates.** Web-search counts, audio and image
  rates, per-request fees: none of them can be derived from what the logs
  record, so a column for them would always be NULL.
* **Context-window tiers.** Some models (Gemini, Claude Opus 4.6 before
  2026-03-13) charge more above a context threshold. The logs record tokens
  per request, not the context length at the time, so the tier cannot be
  chosen honestly; the base rate is taken and the row is annotated. This
  UNDER-states cost for long-context traffic on tiered models, which is
  recorded in the note rather than hidden.
* **Non-date constraints** (`time_of_date` for Deepseek's off-peak pricing).
  Same reason: a clause we cannot evaluate is a clause we must not guess at.

Usage:
    python3 scripts/sync_prices.py            # fetch, rewrite the snapshot
    python3 scripts/sync_prices.py --check    # exit 1 if the snapshot is stale
"""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO = "pydantic/genai-prices"
DATA_URL = f"https://raw.githubusercontent.com/{REPO}/main/prices/data.json"
COMMIT_URL = f"https://api.github.com/repos/{REPO}/commits/main"
OUT = Path(__file__).resolve().parents[1] / "src" / "cc_insights" / "model_prices.json"

#: Provider preference. Our model strings are bare -- a log records
#: `gpt-5.3-codex`, never `openai/gpt-5.3-codex` -- so a string can match
#: several providers' patterns at different prices. First match in this order
#: wins, first-party vendors ahead of resellers, and the chosen provider is
#: recorded on every row so the choice is visible rather than implied.
#: A provider absent from this list is skipped entirely.
PROVIDERS = [
    "anthropic",
    "openai",
    "google",
    "x-ai",
    "deepseek",
    "mistral",
    "moonshotai",
    "zhipuai",
    "minimax",
    "cohere",
    "perplexity",
    "groq",
    "together",
    "fireworks",
    "openrouter",
]

RATE_KEYS = {
    "input_mtok": "input_mtok",
    "output_mtok": "output_mtok",
    "cache_read_mtok": "cache_read_mtok",
    "cache_write_mtok": "cache_write_mtok",
}


def fetch(url: str, accept: str = "application/json") -> Any:
    """GET some JSON, falling back to curl.

    A python.org install ships no CA bundle until someone runs
    `Install Certificates.command`, and this script failing with an SSL error
    on a fresh checkout would read as "the catalog is unreachable". curl uses
    the system trust store and is on every machine this runs on.
    """
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "cc-insights"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https URL
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLError):
            raise
        out = subprocess.run(
            ["curl", "-fsSL", "-H", f"Accept: {accept}", url],
            capture_output=True, check=True,
        )
        return json.loads(out.stdout.decode("utf-8"))


def rate(value: Any) -> tuple[float | None, bool]:
    """One rate, and whether a context tier was flattened to its base."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value), False
    if isinstance(value, dict) and isinstance(value.get("base"), (int, float)):
        return float(value["base"]), bool(value.get("tiers"))
    return None, False


def clauses_for(model: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a model's prices into date-ordered clauses.

    genai-prices writes either one `prices` mapping or a list of
    `{constraint, prices}` clauses. A clause with a constraint we cannot
    evaluate from a timestamp alone is dropped rather than guessed at; a model
    left with no clauses is simply not priced.
    """
    raw = model.get("prices")
    groups = raw if isinstance(raw, list) else [{"prices": raw}]
    out: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("prices"), dict):
            continue
        constraint = group.get("constraint") or {}
        if not isinstance(constraint, dict):
            continue
        unusable = set(constraint) - {"start_date", "end_date"}
        if unusable:
            continue  # time-of-day pricing and friends: cannot be evaluated here
        start = constraint.get("start_date")
        clause: dict[str, Any] = {"start_date": start if isinstance(start, str) else None}
        tiered = False
        for src, dest in RATE_KEYS.items():
            value, was_tiered = rate(group["prices"].get(src))
            clause[dest] = value
            tiered = tiered or was_tiered
        if all(clause[k] is None for k in RATE_KEYS.values()):
            continue
        if tiered:
            clause["tiered"] = True
        out.append(clause)
    out.sort(key=lambda c: c["start_date"] or "")
    return out


def build() -> dict[str, Any]:
    catalog = fetch(DATA_URL)
    try:
        sha = fetch(COMMIT_URL, "application/vnd.github+json")["sha"]
    except Exception as exc:  # provenance is nice to have, not worth failing on
        print(f"warning: could not resolve the source commit ({exc})", file=sys.stderr)
        sha = None

    by_id = {p["id"]: p for p in catalog if isinstance(p, dict) and "id" in p}
    missing = [p for p in PROVIDERS if p not in by_id]
    if missing:
        print(f"warning: catalog has no provider(s) {missing}", file=sys.stderr)

    models: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        for model in by_id.get(provider, {}).get("models", []):
            clauses = clauses_for(model)
            if not clauses or not model.get("match"):
                continue
            models.append({
                "provider": provider,
                "id": model["id"],
                "match": model["match"],
                "clauses": clauses,
            })

    return {
        "source": {
            "repo": REPO,
            "url": DATA_URL,
            "commit": sha,
            "license": "MIT",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "provider_order": PROVIDERS,
        "models": models,
    }


def comparable(snapshot: dict[str, Any]) -> str:
    """The snapshot minus its fetch timestamp, so `--check` tracks content."""
    body = {k: v for k, v in snapshot.items() if k != "source"}
    body["source"] = {k: v for k, v in snapshot["source"].items() if k != "fetched_at"}
    return json.dumps(body, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed snapshot differs from upstream")
    args = ap.parse_args(argv)

    built = build()
    if args.check:
        if not OUT.exists():
            print(f"{OUT} is missing", file=sys.stderr)
            return 1
        current = json.loads(OUT.read_text())
        if comparable(current) == comparable(built):
            print(f"{OUT.name} is up to date ({len(built['models'])} models)")
            return 0
        print(f"{OUT.name} is stale; re-run without --check", file=sys.stderr)
        return 1

    OUT.write_text(json.dumps(built, indent=1, sort_keys=False) + "\n")
    dated = sum(1 for m in built["models"] if len(m["clauses"]) > 1)
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}")
    print(f"  {len(built['models'])} models, {dated} with a price change on record")
    print(f"  {OUT.stat().st_size / 1024:.0f} KB, from {REPO}@{built['source']['commit'] or '?'}")
    print(f"  today is {date.today()}; prices dated after it are ignored at query time")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
