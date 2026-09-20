#!/usr/bin/env python3
"""Capture every API endpoint from a live database into frontend fixtures.

    python3 scripts/dump_fixtures.py [--config-dir DIR] [--out DIR]

The frontend renders from these with no server running, so the UI can be built
and reviewed independently of the backend, and `tests/test_serve.py` asserts
that a live server still answers exactly what is committed here.

**This used to reimplement all nine endpoints in SQL.** It was written before
`metrics.py` existed, so that WP8 and WP9 could proceed in parallel against
one contract; once both sides shipped, the second implementation stopped
being a cross-check and became a place for the two to drift -- adding cost
would have meant writing the day-bucketing and the nano-unit rounding twice.
It now calls `metrics.endpoint()`, the same function `cci serve` calls. The
fixtures keep their regression value: they are a committed snapshot of what
this pipeline answered on a real corpus, and a diff here is a real change in
behaviour that a human should look at before committing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cc_insights import config as config_mod, db, metrics  # noqa: E402

OUT_DEFAULT = Path(__file__).resolve().parents[1] / "frontend" / "src" / "fixtures"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = ap.parse_args()

    cfg = config_mod.load(args.config_dir, create=False)
    if not cfg.db_path.exists():
        print(f"no database at {cfg.db_path} — run `cci init`", file=sys.stderr)
        return 1
    conn = db.connect(cfg.db_path)
    args.out.mkdir(parents=True, exist_ok=True)

    unfiltered = metrics.Filters()
    for name in metrics.ENDPOINTS:
        payload = metrics.endpoint(name, conn, unfiltered, cfg)
        target = args.out / f"{name}.json"
        target.write_text(json.dumps(payload, indent=1) + "\n")
        print(f"  {target.name:<16} {target.stat().st_size / 1024:>8.1f} KB")
    conn.close()
    print(f"wrote {len(metrics.ENDPOINTS)} fixtures to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
