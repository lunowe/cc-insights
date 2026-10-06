#!/usr/bin/env python3
"""Compare the Claude Code, Codex and opencode adapters' token totals with ccusage's.

Adapter level only: this parses the logs on this machine with the adapters,
attributes models the way `cost.py` does, and sums tokens per model. It never
touches a database, and it reads the logs read-only.

    python scripts/parity_ccusage.py                 # runs npx ccusage itself
    python scripts/parity_ccusage.py --codex-json c.json --opencode-json o.json

ccusage's numbers come from

    npx -y ccusage@latest claude monthly --json --breakdown --offline
    npx -y ccusage@latest codex monthly --json --breakdown --offline
    npx -y ccusage@latest opencode monthly --json --breakdown --offline

and are summed over months. What the columns mean on each side:

* ``input`` is fresh input: Codex's ``input_tokens`` minus cached and cache
  writes. ccusage's ``inputTokens`` is the same quantity.
* ``output`` is what is billed at the output rate. For opencode that includes
  separately reported reasoning; ccusage leaves it out of ``outputTokens`` but
  bills it, so the ccusage column here is ``totalTokens - input - cache``.

Known differences, both deliberate (measured 2026-10-06):

* **Codex vs ccusage 20.0.26 (npm).** That release predates ccusage's
  compaction counting (ccusage PR #1821, merged 2026-10-02), so it leaves out
  remote compaction requests that the adapter, like ccusage's main branch,
  counts. Against a build of ccusage's main branch the totals are identical.
  The adapter departs from ccusage in two corner cases that do not occur in
  the measured logs: a compaction billed before its own advancing
  token_count, and repeated snapshots taken as replay-burst evidence (see
  ``sources/codex.py``).
* **opencode gpt-5.3-codex output.** ccusage adds ``reasoning`` to output on
  every message. opencode 1.2 already counted reasoning inside ``output``
  (its ``total`` leaves reasoning out), so the adapter does not add it again
  for those messages. See ``_billed_output`` in ``sources/opencode.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cc_insights.sources.base import RawEvent  # noqa: E402
from cc_insights.sources.claude_code import ClaudeCodeAdapter  # noqa: E402
from cc_insights.sources.codex import CodexAdapter  # noqa: E402
from cc_insights.sources.opencode import OpencodeAdapter  # noqa: E402

FIELDS = ("input", "cache_read", "cache_write", "output")
Totals = dict[str, dict[str, int]]


# --------------------------------------------------------------------------
# our side
# --------------------------------------------------------------------------


def _events(adapter: Any) -> list[tuple[int, RawEvent]]:
    """Every event, deduped on (session, native_event_id) as ingest does, with
    its position in parse order (ingest's row id breaks timestamp ties)."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[int, RawEvent]] = []
    for path in adapter.discover():
        for ev in adapter.parse(path):
            key = (ev.native_session_id, ev.native_event_id)
            if key in seen:
                continue
            seen.add(key)
            out.append((len(out), ev))
    return out


def totals_by_model(events: Iterable[tuple[int, RawEvent]]) -> Totals:
    """Tokens per model, carrying the last named model forward within a
    thread in (ts, ordinal, row) order -- the rule in `cost.py`."""
    rows = sorted(
        events,
        key=lambda pair: (
            pair[1].native_thread_id,
            pair[1].ts_ms,
            pair[1].ordinal is not None,  # SQLite sorts NULL first
            pair[1].ordinal or 0,
            pair[0],
        ),
    )
    totals: Totals = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    thread, carried = None, None
    for _, ev in rows:
        if ev.native_thread_id != thread:
            thread, carried = ev.native_thread_id, None
        if ev.model:
            carried = ev.model
        values = (ev.input_tokens, ev.cache_read_tokens, ev.cache_write_tokens,
                  ev.output_tokens)
        if not any(values):
            continue
        bucket = totals[ev.model or carried or "(no model)"]
        for field, value in zip(FIELDS, values):
            bucket[field] += value or 0
    return dict(totals)


def claude_totals(adapter: Any | None = None) -> Totals:
    return totals_by_model(_events(adapter or ClaudeCodeAdapter()))


def codex_totals(adapter: Any | None = None) -> Totals:
    return totals_by_model(_events(adapter or CodexAdapter()))


def opencode_totals(adapter: Any | None = None) -> Totals:
    return totals_by_model(_events(adapter or OpencodeAdapter()))


# --------------------------------------------------------------------------
# ccusage's side
# --------------------------------------------------------------------------


def _ccusage(source: str, saved: str | None) -> dict[str, Any]:
    if saved:
        return json.loads(Path(saved).read_text(encoding="utf-8"))
    cmd = ["npx", "-y", "ccusage@latest", source, "monthly", "--json",
           "--breakdown", "--offline"]
    done = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def ccusage_claude(doc: dict[str, Any]) -> Totals:
    """Per model, from the monthly breakdown. ccusage reports one cache-write
    figure (5m and 1h together), which is what `cache_write` holds here too."""
    totals: Totals = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for month in doc.get("monthly", []):
        for row in month.get("modelBreakdowns", []):
            bucket = totals[row["modelName"]]
            bucket["input"] += row.get("inputTokens", 0)
            bucket["cache_read"] += row.get("cacheReadTokens", 0)
            bucket["cache_write"] += row.get("cacheCreationTokens", 0)
            bucket["output"] += row.get("outputTokens", 0)
    return dict(totals)


def ccusage_codex(doc: dict[str, Any]) -> Totals:
    totals: Totals = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for month in doc.get("monthly", []):
        for model, row in month.get("models", {}).items():
            bucket = totals[model]
            bucket["input"] += row.get("inputTokens", 0)
            bucket["cache_read"] += row.get("cacheReadTokens", 0)
            bucket["cache_write"] += row.get("cacheCreationTokens", 0)
            bucket["output"] += row.get("outputTokens", 0)
    return dict(totals)


def ccusage_opencode(doc: dict[str, Any]) -> Totals:
    """Per model. ccusage's per-model rows carry no total, so the billed
    output (output + reasoning) is recovered from each month's total, which
    is exact for a month with one model and flagged otherwise."""
    totals: Totals = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    for month in doc.get("monthly", []):
        rows = month.get("modelBreakdowns", [])
        for row in rows:
            bucket = totals[row["modelName"]]
            bucket["input"] += row.get("inputTokens", 0)
            bucket["cache_read"] += row.get("cacheReadTokens", 0)
            bucket["cache_write"] += row.get("cacheCreationTokens", 0)
            output = row.get("outputTokens", 0)
            if len(rows) == 1:
                output = (month.get("totalTokens", 0) - month.get("inputTokens", 0)
                          - month.get("cacheReadTokens", 0)
                          - month.get("cacheCreationTokens", 0))
            bucket["output"] += output
    return dict(totals)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def table(title: str, columns: dict[str, Totals]) -> str:
    names = list(columns)
    models = sorted({m for t in columns.values() for m in t})
    lines = [f"## {title}", ""]
    header = ["model", "field", *names]
    if len(names) >= 2:
        header.append(f"{names[-2]} - {names[-1]}")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for model in models:
        for field in FIELDS:
            values = [columns[n].get(model, {}).get(field, 0) for n in names]
            if not any(values):
                continue
            row = [model, field, *(f"{v:,}" for v in values)]
            if len(names) >= 2:
                row.append(f"{values[-2] - values[-1]:+,}")
            lines.append("| " + " | ".join(row) + " |")
    sums = {n: {f: sum(t.get(f, 0) for t in columns[n].values()) for f in FIELDS}
            for n in names}
    for field in FIELDS:
        values = [sums[n][field] for n in names]
        row = ["**all**", field, *(f"{v:,}" for v in values)]
        if len(names) >= 2:
            row.append(f"{values[-2] - values[-1]:+,}")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claude-json", help="saved `ccusage claude monthly --json --breakdown` output")
    parser.add_argument("--codex-json", help="saved `ccusage codex monthly --json --breakdown` output")
    parser.add_argument("--opencode-json", help="saved `ccusage opencode monthly --json --breakdown` output")
    parser.add_argument("--skip-opencode", action="store_true")
    args = parser.parse_args(argv)

    print(table("Claude Code", {
        "ours": claude_totals(),
        "ccusage": ccusage_claude(_ccusage("claude", args.claude_json)),
    }))
    print()
    print(table("Codex", {
        "ours": codex_totals(),
        "ccusage": ccusage_codex(_ccusage("codex", args.codex_json)),
    }))
    if not args.skip_opencode:
        print()
        print(table("opencode", {
            "ours": opencode_totals(),
            "ccusage": ccusage_opencode(_ccusage("opencode", args.opencode_json)),
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
