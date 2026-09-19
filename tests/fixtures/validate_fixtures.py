#!/usr/bin/env python3
"""Validates the WP1 fixture corpus. Exit 0 = fixtures are correct.

Fixtures are not decoration: each one pins a specific trap discovered in
docs/FINDINGS.md. This validator asserts the trap is actually PRESENT in the
fixture, so an adapter that mishandles it will fail a test rather than pass a
fixture that quietly lost the hazard.

Run: python3 tests/fixtures/validate_fixtures.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CC = ROOT / "claude_code"
CX = ROOT / "codex"
errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def lines(p: Path, *, allow_truncated: bool = False) -> list[dict]:
    if not p.exists():
        fail(f"MISSING FILE: {p.relative_to(ROOT)}")
        return []
    out, raw = [], p.read_text().splitlines()
    for i, ln in enumerate(raw):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            if allow_truncated and i == len(raw) - 1:
                continue
            fail(f"{p.relative_to(ROOT)}:{i + 1} invalid JSON")
    return out


def check_claude_resume_replay() -> None:
    """FINDINGS §2: the same event uuid appears in two files with IDENTICAL
    timestamp and type. Dedup on uuid must collapse them."""
    a = lines(CC / "resume_replay_a.jsonl")
    b = lines(CC / "resume_replay_b.jsonl")
    if not a or not b:
        return
    sa = {e.get("sessionId") for e in a}
    sb = {e.get("sessionId") for e in b}
    if sa != sb or len(sa) != 1:
        fail("resume_replay: both files must contain exactly one shared sessionId")
    ua = {e["uuid"]: (e["timestamp"], e["type"]) for e in a if e.get("uuid")}
    ub = {e["uuid"]: (e["timestamp"], e["type"]) for e in b if e.get("uuid")}
    shared = set(ua) & set(ub)
    if not shared:
        fail("resume_replay: no uuid appears in BOTH files — the replay trap is absent")
    for u in shared:
        if ua[u] != ub[u]:
            fail(f"resume_replay: uuid {u[:8]} differs across files; replay must be identical")


def check_claude_missing_uuid() -> None:
    """FINDINGS §2: queue-operation / pr-link / file-history-delta carry no uuid."""
    ev = lines(CC / "missing_uuid.jsonl")
    kinds = {e.get("type") for e in ev if "uuid" not in e}
    if not kinds:
        fail("missing_uuid: no event lacks a uuid — the fallback trap is absent")


def check_claude_idle_gap() -> None:
    """A gap far above the 300s threshold must split one session into 2+ spans."""
    ev = sorted(lines(CC / "idle_gap.jsonl"), key=lambda e: e.get("timestamp", ""))
    if len(ev) < 4:
        fail("idle_gap: need >=4 events")
        return
    from datetime import datetime
    ts = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp() for e in ev]
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    if max(gaps) < 3600:
        fail(f"idle_gap: largest gap is {max(gaps):.0f}s — needs a multi-hour gap")
    if not any(g < 300 for g in gaps):
        fail("idle_gap: needs some sub-threshold gaps too, or there is nothing to group")


def check_claude_agent_pair() -> None:
    """FINDINGS §3: Claude subagents are derived from Agent tool_use/tool_result."""
    ev = lines(CC / "agent_subagent.jsonl")
    uses, results = {}, set()
    for e in ev:
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") in ("Agent", "Task"):
                uses[c.get("id")] = e.get("timestamp")
            elif c.get("type") == "tool_result":
                results.add(c.get("tool_use_id"))
    if not uses:
        fail("agent_subagent: no Agent tool_use block found")
    if not (set(uses) & results):
        fail("agent_subagent: no tool_result matches an Agent tool_use id")
    for e in ev:
        if e.get("isSidechain"):
            fail("agent_subagent: isSidechain must be false — that is the documented trap")


def check_claude_synthetic() -> None:
    ev = lines(CC / "synthetic_model.jsonl")
    if not any((e.get("message") or {}).get("model") == "<synthetic>" for e in ev):
        fail("synthetic_model: no event carries model '<synthetic>'")


def check_claude_truncated() -> None:
    p = CC / "truncated.jsonl"
    if not p.exists():
        fail("MISSING FILE: claude_code/truncated.jsonl")
        return
    raw = p.read_text().splitlines()
    good = lines(p, allow_truncated=True)
    try:
        json.loads(raw[-1])
        fail("truncated: final line parses — it must be a cut-off fragment")
    except json.JSONDecodeError:
        pass
    if len(good) < 2:
        fail("truncated: needs >=2 complete events before the fragment")


def check_claude_overlap() -> None:
    """Two sessions active at the same wall-clock time -> concurrency == 2."""
    from datetime import datetime
    spans = {}
    for name in ("overlap_a.jsonl", "overlap_b.jsonl"):
        ev = lines(CC / name)
        if not ev:
            return
        ts = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
              for e in ev if e.get("timestamp")]
        sids = {e.get("sessionId") for e in ev}
        if len(sids) != 1:
            fail(f"{name}: must contain exactly one sessionId")
        spans[name] = (min(ts), max(ts), sids.pop())
    if len(spans) == 2:
        (a0, a1, sa), (b0, b1, sb) = spans.values()
        if sa == sb:
            fail("overlap: the two files must be DIFFERENT sessions")
        if not (a0 < b1 and b0 < a1):
            fail("overlap: the two sessions do not overlap in time")


def check_codex_thread_hierarchy() -> None:
    """FINDINGS §2+§3: two threads share a session_id, have DIFFERENT payload.id,
    and reuse the same ordinals with different timestamps. Keying on ordinal
    alone silently drops events -- the fixture must make that fail loudly."""
    root = lines(CX / "root_thread.jsonl")
    sub = lines(CX / "subagent_thread.jsonl")
    if not root or not sub:
        return

    def meta(ev, name):
        for e in ev:
            if e.get("type") == "session_meta":
                return e.get("payload") or {}
        fail(f"{name}: no session_meta line")
        return {}

    mr, ms = meta(root, "root_thread"), meta(sub, "subagent_thread")
    if not mr or not ms:
        return
    if mr.get("session_id") != ms.get("session_id"):
        fail("codex: both threads must share one session_id")
    if mr.get("id") == ms.get("id"):
        fail("codex: threads must have DIFFERENT payload.id")
    if "subagent" not in (ms.get("source") or {}):
        fail("codex: subagent_thread's payload.source must contain key 'subagent'")
    if ms.get("parent_thread_id") != mr.get("id"):
        fail("codex: subagent parent_thread_id must point at the root thread id")

    def ords(ev):
        return {e["ordinal"]: e["timestamp"] for e in ev if e.get("ordinal") is not None}

    orr, ors = ords(root), ords(sub)
    shared = set(orr) & set(ors)
    if not shared:
        fail("codex: threads share no ordinal values — the collision trap is absent")
    if not any(orr[o] != ors[o] for o in shared):
        fail("codex: shared ordinals have identical timestamps — must be DIFFERENT events")


def check_codex_truncated() -> None:
    p = CX / "truncated.jsonl"
    if not p.exists():
        fail("MISSING FILE: codex/truncated.jsonl")
        return
    raw = p.read_text().splitlines()
    try:
        json.loads(raw[-1])
        fail("codex/truncated: final line parses — it must be a cut-off fragment")
    except json.JSONDecodeError:
        pass


def main() -> int:
    for fn in (
        check_claude_resume_replay, check_claude_missing_uuid, check_claude_idle_gap,
        check_claude_agent_pair, check_claude_synthetic, check_claude_truncated,
        check_claude_overlap, check_codex_thread_hierarchy, check_codex_truncated,
    ):
        try:
            fn()
        except Exception as e:  # a malformed fixture must not crash the validator
            fail(f"{fn.__name__} raised {type(e).__name__}: {e}")

    if errors:
        print(f"FAILED — {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK — all fixtures valid and every documented trap is present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
