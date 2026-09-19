"""Canonical reference implementation of CC-Insights time math.

THIS FILE IS THE SPEC. WP5 must reproduce its output.

Active time = sum of ACTIVE SPAN durations, where a span is a maximal run of
events whose consecutive gaps are <= idle_threshold. A gap exceeding the
threshold contributes ZERO -- it is NOT capped-and-counted. The `sum(min(gap,T))`
form invents T seconds per idle gap and inflated an early draft by 59%.

Spans are computed PER THREAD. A thread is one serial event stream; a session
groups the root thread with its subagent threads. Computing spans per session
would merge concurrent subagents into one stream and hide the parallelism that
is the whole point of this tool.

Three bugs were found in the first version of this file, each of which had
already propagated into docs/FINDINGS.md as "ground truth":
  1. Claude session fallback used the file BASENAME (with .jsonl) instead of the
     stem, minting 3 phantom sessions (121 vs the true 118).
  2. Codex used the BARE ordinal as the dedup key -- the exact trap FINDINGS §2
     documents. Ordinals are thread-scoped, so sibling threads collided and
     8,709 real events were silently dropped (54,296 vs the true 63,005).
  3. The Claude glob `projects/*/*.jsonl` misses `projects/*/*/subagents/**`,
     which holds 59,667 events -- 52% of all Claude Code activity.
"""
from __future__ import annotations

import collections
import glob
import hashlib
import json
import os
import sys
from datetime import datetime

IDLE = 300  # seconds

CLAUDE_MAIN = "~/.claude/projects/*/*.jsonl"
CLAUDE_SUBAGENTS = "~/.claude/projects/*/*/subagents/**/*.jsonl"
CODEX_GLOBS = ["~/.codex/sessions/*/*/*/*.jsonl", "~/.codex/archived_sessions/**/*.jsonl"]


def _ts(raw: str) -> float | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_claude() -> dict[tuple[str, str], dict[str, float]]:
    """-> {(session_id, thread_id): {native_event_id: ts}}"""
    ev: dict[tuple[str, str], dict[str, float]] = collections.defaultdict(dict)

    for f in glob.glob(os.path.expanduser(CLAUDE_MAIN)):
        # Session fallback uses the file STEM: the 36 `file-history-delta`
        # events carry no sessionId, and their stems are real session ids.
        fallback = os.path.basename(f)[:-6]
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = _ts(d.get("timestamp") or "")
            if t is None:
                continue
            sid = d.get("sessionId") or fallback
            nid = d.get("uuid") or hashlib.sha256(line.encode()).hexdigest()[:32]
            ev[(sid, sid)][nid] = t  # root thread: thread_id == session_id

    for f in glob.glob(os.path.expanduser(CLAUDE_SUBAGENTS), recursive=True):
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = _ts(d.get("timestamp") or "")
            if t is None:
                continue
            sid = d.get("sessionId") or os.path.basename(f)[:-6]
            tid = d.get("agentId") or os.path.basename(f)[:-6]
            nid = d.get("uuid") or hashlib.sha256(line.encode()).hexdigest()[:32]
            ev[(sid, tid)][nid] = t
    return ev


def load_codex() -> dict[tuple[str, str], dict[str, float]]:
    ev: dict[tuple[str, str], dict[str, float]] = collections.defaultdict(dict)
    files: list[str] = []
    for g in CODEX_GLOBS:
        files += glob.glob(os.path.expanduser(g), recursive=True)

    for f in files:
        # Only the FIRST session_meta is authoritative: 3 files carry a second
        # one whose payload.id is the root session id, not the thread id.
        sid = tid = None
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "session_meta":
                p = d.get("payload") or {}
                sid, tid = p.get("session_id"), p.get("id")
                break
        stem = os.path.basename(f)[:-6]
        sid, tid = sid or stem, tid or stem

        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            t = _ts(d.get("timestamp") or "")
            if t is None:
                continue
            o = d.get("ordinal")
            # Thread-scoped key. The bare ordinal drops 8,709 events.
            nid = f"{tid}:{o}" if o is not None else hashlib.sha256(line.encode()).hexdigest()[:32]
            ev[(sid, tid)][nid] = t
    return ev


def spans(ev, idle: int = IDLE):
    """-> [(start, end, session_id, thread_id)] maximal active runs per thread."""
    out = []
    for (sid, tid), m in ev.items():
        T = sorted(m.values())
        if len(T) < 2:
            continue
        start = prev = T[0]
        for t in T[1:]:
            if t - prev > idle:
                out.append((start, prev, sid, tid))
                start = t
            prev = t
        out.append((start, prev, sid, tid))
    return out


def concurrency(iv):
    pts = sorted([(a, 1) for a, b, *_ in iv] + [(b, -1) for a, b, *_ in iv])
    cur = 0
    last = None
    at = collections.Counter()
    peak = 0
    pw = None
    for t, delta in pts:
        if last is not None and cur > 0:
            at[cur] += t - last
        cur += delta
        last = t
        if cur > peak:
            peak, pw = cur, t
    return at, peak, pw, sum(at.values())


def report(source: str):
    ev = load_claude() if source == "claude_code" else load_codex()
    iv = spans(ev)
    active = sum(b - a for a, b, *_ in iv)
    at, peak, pw, wall = concurrency(iv)
    allts = [t for m in ev.values() for t in m.values()]
    sessions = {s for s, _ in ev}
    print(f"\n=== {source} ===")
    print(f"sessions            : {len(sessions)}")
    print(f"threads             : {len(ev)}")
    print(f"events (deduped)    : {sum(len(m) for m in ev.values()):,}")
    print(f"coverage            : {datetime.fromtimestamp(min(allts)):%Y-%m-%d} .. "
          f"{datetime.fromtimestamp(max(allts)):%Y-%m-%d}")
    print(f"active spans        : {len(iv)}")
    print(f"ACTIVE TIME         : {active/3600:.1f} h")
    print(f"wall-clock >=1      : {wall/3600:.1f} h")
    print(f"parallel multiplier : {active/wall:.2f}x")
    print(f"peak concurrency    : {peak} at {datetime.fromtimestamp(pw):%Y-%m-%d %H:%M}")
    ge2 = sum(v for k, v in at.items() if k >= 2)
    print(f">=2 concurrent      : {ge2/3600:.1f} h ({100*ge2/wall:.0f}%)")
    for k in sorted(at):
        print(f"   {k:>2} concurrent     : {at[k]/3600:6.1f} h ({100*at[k]/wall:4.1f}%)")
    return ev, iv


if __name__ == "__main__":
    for s in (sys.argv[1:] or ["claude_code", "codex"]):
        report(s)
