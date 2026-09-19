"""Canonical reference implementation of CC-Insights time math.

Active time = sum of ACTIVE SPAN durations, where a span is a maximal run of
events whose consecutive gaps are <= idle_threshold. A gap exceeding the
threshold contributes ZERO -- it is not capped-and-counted. Capping (the
`sum(min(gap, T))` form) silently invents T seconds per idle gap and inflated
an early draft of these numbers by ~60%.

This file is the spec. WP4 must reproduce it.
"""
import json, glob, os, collections, hashlib, sys
from datetime import datetime

IDLE = 300  # seconds


def load(source):
    """-> {session_id: {native_event_id: ts_seconds}}, deduped."""
    ev = collections.defaultdict(dict)
    if source == "claude_code":
        files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    else:
        files = glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")) \
              + glob.glob(os.path.expanduser("~/.codex/archived_sessions/**/*.jsonl"), recursive=True)
    for f in files:
        # Codex: only the session_meta line carries session_id -- resolve it once
        # per file and apply to every event in that file. Falling back per-line
        # splits each file into a phantom extra session.
        file_sid = None
        if source == "codex":
            for line in open(f, errors="ignore"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pl = d.get("payload") or {}
                file_sid = pl.get("session_id") or d.get("session_id")
                if file_sid:
                    break
            if not file_sid:
                file_sid = os.path.basename(f)

        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            raw_ts = d.get("timestamp")
            if not raw_ts:
                continue
            try:
                t = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if source == "claude_code":
                sid = d.get("sessionId") or os.path.basename(f)
                nid = d.get("uuid") or hashlib.sha256(line.encode()).hexdigest()[:32]
            else:
                sid = file_sid
                o = d.get("ordinal")
                nid = str(o) if o is not None else hashlib.sha256(line.encode()).hexdigest()[:32]
            ev[sid][nid] = t
    return ev


def spans(ev, idle=IDLE):
    """-> [(start, end, session_id)] maximal active runs."""
    out = []
    for sid, m in ev.items():
        T = sorted(m.values())
        if len(T) < 2:
            continue
        start = prev = T[0]
        for t in T[1:]:
            if t - prev > idle:
                out.append((start, prev, sid))
                start = t
            prev = t
        out.append((start, prev, sid))
    return out


def concurrency(iv):
    """-> (time_at_level, peak, peak_ts, wall_clock_with_ge_1)."""
    pts = sorted([(a, 1) for a, b, _ in iv] + [(b, -1) for a, b, _ in iv])
    cur = 0; last = None; at = collections.Counter(); peak = 0; pw = None
    for t, delta in pts:
        if last is not None and cur > 0:
            at[cur] += t - last
        cur += delta; last = t
        if cur > peak:
            peak, pw = cur, t
    return at, peak, pw, sum(at.values())


def report(source):
    ev = load(source)
    iv = spans(ev)
    active = sum(b - a for a, b, _ in iv)
    at, peak, pw, wall = concurrency(iv)
    allts = [t for m in ev.values() for t in m.values()]
    wallspan = sum(max(m.values()) - min(m.values()) for m in ev.values() if len(m) > 1)
    print(f"\n=== {source} ===")
    print(f"sessions            : {len(ev)}")
    print(f"events (deduped)    : {sum(len(m) for m in ev.values()):,}")
    print(f"coverage            : {datetime.fromtimestamp(min(allts)):%Y-%m-%d} .. {datetime.fromtimestamp(max(allts)):%Y-%m-%d}")
    print(f"active spans        : {len(iv)}")
    print(f"ACTIVE TIME         : {active/3600:.1f} h")
    print(f"sum of wall-spans   : {wallspan/3600:.1f} h  (active is {100*active/wallspan:.1f}% of it)")
    print(f"wall-clock >=1      : {wall/3600:.1f} h")
    print(f"parallel multiplier : {active/wall:.2f}x")
    print(f"peak concurrency    : {peak} at {datetime.fromtimestamp(pw):%Y-%m-%d %H:%M}")
    print(f">=2 concurrent      : {sum(v for k,v in at.items() if k>=2)/3600:.1f} h ({100*sum(v for k,v in at.items() if k>=2)/wall:.0f}%)")
    for k in sorted(at):
        print(f"   {k} concurrent      : {at[k]/3600:6.1f} h ({100*at[k]/wall:4.1f}%)")
    return ev, iv


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["claude_code", "codex"]):
        report(s)
