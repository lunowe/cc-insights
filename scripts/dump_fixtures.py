#!/usr/bin/env python3
"""Capture every API endpoint from a live database into frontend fixtures.

    python3 scripts/dump_fixtures.py [--config-dir DIR] [--out DIR]

The frontend must render from these with no server running, so the UI can be
built and reviewed independently of the backend. Shapes here are the contract in
docs/API.md -- if the two disagree, the contract wins.

Spans are SPLIT at local day and hour boundaries for the daily and heatmap
series. A span that runs past midnight belongs partly to each day; attributing
it whole to its start day silently misplaces long overnight runs.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cc_insights import config as config_mod, db  # noqa: E402

TIMELINE_LIMIT = 4000


def rows(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]


def one(conn, sql, *a):
    r = conn.execute(sql, a).fetchone()
    return dict(r) if r else {}


def meta(conn, cfg):
    m = one(conn, "SELECT hostname FROM host ORDER BY last_seen DESC LIMIT 1")
    span = one(conn, "SELECT min(ts) lo, max(ts) hi FROM event")
    return {
        "hostname": m.get("hostname", "unknown"),
        "firstTs": span.get("lo"),
        "lastTs": span.get("hi"),
        "sources": [r["source"] for r in rows(conn, "SELECT DISTINCT source FROM session ORDER BY 1")],
        "projects": rows(conn, """
            SELECT p.project_id projectId, p.name, p.root_path rootPath,
                   coalesce(sum(sp.ended_at - sp.started_at), 0) activeMs
            FROM project p
            LEFT JOIN session s ON s.project_id = p.project_id
            LEFT JOIN span sp ON sp.session_id = s.id
            GROUP BY 1, 2, 3 ORDER BY activeMs DESC"""),
        # Every group is offered as a filter choice, including one with no
        # time yet -- `groups.json` is the ranking, this is the roster.
        "groups": rows(conn, """
            SELECT g.group_id groupId, g.name,
                   (SELECT coalesce(sum(sp.ended_at - sp.started_at), 0)
                      FROM span sp
                      JOIN session s ON s.id = sp.session_id
                      JOIN project p ON p.project_id = s.project_id
                     WHERE p.group_id = g.group_id) activeMs
            FROM project_group g ORDER BY activeMs DESC, g.name"""),
        "agents": rows(conn, """
            SELECT DISTINCT t.agent_name agentName, s.source
            FROM thread t JOIN session s ON s.id = t.session_id
            WHERE t.agent_name IS NOT NULL ORDER BY 1"""),
        "models": [r["model"] for r in rows(conn,
            "SELECT DISTINCT model FROM event WHERE model IS NOT NULL ORDER BY 1")],
        "idleThresholdS": cfg.idle_threshold_s,
        "generatedAt": db.now_ms(),
    }


def summary(conn):
    def bucket(where):
        return one(conn, f"""SELECT coalesce(sum(sp.ended_at - sp.started_at), 0) v
            FROM span sp JOIN thread t ON t.id = sp.thread_id WHERE {where}""")["v"]
    tok = one(conn, """SELECT coalesce(sum(input_tokens),0) input,
        coalesce(sum(output_tokens),0) output, coalesce(sum(cache_read_tokens),0) cacheRead,
        coalesce(sum(cache_write_tokens),0) cacheWrite FROM event""")
    # Counts are of rows REACHABLE FROM THE SURVIVING SPANS, not global counts
    # (docs/API.md). Under role=subagent a global session total would describe
    # none of the numbers printed beside it. Costs one row each today: the
    # corpus holds exactly one thread with a single event, which correctly
    # yields no span.
    return {
        "sessions": one(conn, "SELECT count(DISTINCT session_id) v FROM span")["v"],
        "threads": one(conn, "SELECT count(DISTINCT thread_id) v FROM span")["v"],
        "events": one(conn, """SELECT count(*) v FROM event e WHERE EXISTS (
            SELECT 1 FROM span sp WHERE sp.thread_id = e.thread_id
              AND e.ts >= sp.started_at AND e.ts <= sp.ended_at)""")["v"],
        "spans": one(conn, "SELECT count(*) v FROM span")["v"],
        "activeMs": one(conn, "SELECT coalesce(sum(ended_at - started_at),0) v FROM span")["v"],
        "bySource": rows(conn, """SELECT s.source, coalesce(sum(sp.ended_at - sp.started_at),0) activeMs
            FROM span sp JOIN session s ON s.id = sp.session_id GROUP BY 1 ORDER BY 2 DESC"""),
        "humanInitiatedMs": bucket("t.is_subagent = 0 AND sp.attended = 1"),
        "autonomousMs": bucket("t.is_subagent = 1"),
        "unattendedRootMs": bucket("t.is_subagent = 0 AND (sp.attended = 0 OR sp.attended IS NULL)"),
        "tokens": tok,
    }


def timeline(conn):
    all_spans = rows(conn, """
        SELECT sp.id spanId, sp.thread_id threadId, sp.session_id sessionId,
               s.project_id projectId, p.name projectName, s.source,
               t.agent_name agentName, t.is_subagent isSubagent,
               t.parent_thread_id parentThreadId,
               sp.attended, sp.started_at start, sp.ended_at end
        FROM span sp
        JOIN thread t ON t.id = sp.thread_id
        JOIN session s ON s.id = sp.session_id
        LEFT JOIN project p ON p.project_id = s.project_id
        ORDER BY sp.started_at""")
    for r in all_spans:
        r["isSubagent"] = bool(r["isSubagent"])
    truncated = len(all_spans) > TIMELINE_LIMIT
    if truncated:
        all_spans = sorted(all_spans, key=lambda r: r["end"] - r["start"], reverse=True)[:TIMELINE_LIMIT]
        all_spans.sort(key=lambda r: r["start"])
    return {"spans": all_spans, "truncated": truncated, "limit": TIMELINE_LIMIT}


def _slice_by(conn, key):
    """Split every span at local boundaries; key(dt) -> bucket."""
    acc = collections.Counter()
    per_src = collections.defaultdict(collections.Counter)
    for r in conn.execute("""SELECT sp.started_at a, sp.ended_at b, s.source
                             FROM span sp JOIN session s ON s.id = sp.session_id"""):
        a, b, src = r["a"], r["b"], r["source"]
        cur = a
        while cur < b:
            dt = datetime.fromtimestamp(cur / 1000)
            nxt_dt = key.next_boundary(dt)
            nxt = min(b, int(nxt_dt.timestamp() * 1000))
            if nxt <= cur:
                break
            acc[key.label(dt)] += nxt - cur
            per_src[key.label(dt)][src] += nxt - cur
            cur = nxt
        if a == b:  # zero-duration span still marks presence
            acc[key.label(datetime.fromtimestamp(a / 1000))] += 0
    return acc, per_src


class _Day:
    @staticmethod
    def next_boundary(dt): return (dt.replace(hour=0, minute=0, second=0, microsecond=0)
                                   + timedelta(days=1))
    @staticmethod
    def label(dt): return dt.strftime("%Y-%m-%d")


class _Hour:
    @staticmethod
    def next_boundary(dt): return (dt.replace(minute=0, second=0, microsecond=0)
                                   + timedelta(hours=1))
    @staticmethod
    def label(dt): return (dt.weekday(), dt.hour)


def _union_ms(intervals: list[tuple[int, int]]) -> int:
    """Length of the union of intervals -- overlapping work counted once."""
    total = 0
    cur_a = cur_b = None
    for a, b in sorted(intervals):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total


def daily(conn):
    acc, per_src = _slice_by(conn, _Day)
    # wallMs is the UNION of that day's spans, not the sum: on a day with two
    # agents running at once, active time exceeds elapsed time. Emitting the sum
    # for both makes the parallelism invisible, which is the one thing this
    # tool exists to show.
    per_day_iv = collections.defaultdict(list)
    for r in conn.execute("SELECT started_at a, ended_at b FROM span"):
        a, b = r["a"], r["b"]
        cur = a
        while cur < b:
            dt = datetime.fromtimestamp(cur / 1000)
            nxt = min(b, int(_Day.next_boundary(dt).timestamp() * 1000))
            if nxt <= cur:
                break
            per_day_iv[_Day.label(dt)].append((cur, nxt))
            cur = nxt
    wall = {k: _union_ms(v) for k, v in per_day_iv.items()}

    if not acc:
        return {"days": []}
    lo = datetime.strptime(min(acc), "%Y-%m-%d").date()
    hi = datetime.strptime(max(acc), "%Y-%m-%d").date()
    out, d = [], lo
    while d <= hi:  # fill gaps so the chart has no invisible holes
        k = d.strftime("%Y-%m-%d")
        out.append({"date": k, "activeMs": acc.get(k, 0), "wallMs": wall.get(k, 0),
                    "bySource": dict(per_src.get(k, {}))})
        d += timedelta(days=1)
    return {"days": out}


def heatmap(conn):
    acc, _ = _slice_by(conn, _Hour)
    return {"cells": [{"weekday": w, "hour": h, "activeMs": ms} for (w, h), ms in sorted(acc.items())]}


def concurrency(conn):
    iv = [(r["a"], r["b"]) for r in conn.execute(
        "SELECT started_at a, ended_at b FROM span ORDER BY started_at")]
    pts = sorted([(a, 1) for a, b in iv] + [(b, -1) for a, b in iv])
    cur = last = 0
    at = collections.Counter(); peak = 0; peak_at = None
    last = None
    for t, d in pts:
        if last is not None and cur > 0:
            at[cur] += t - last
        cur += d; last = t
        if cur > peak:
            peak, peak_at = cur, t
    wall = sum(at.values()); active = sum(b - a for a, b in iv)
    return {"timeAtLevel": {str(k): v for k, v in sorted(at.items())}, "peak": peak,
            "peakAt": peak_at, "wallMs": wall, "activeMs": active,
            "multiplier": round(active / wall, 4) if wall else 0}


def projects(conn):
    # groupId/groupName are a LEFT JOIN: an ungrouped project is legal and
    # reports both null. groupPinned = a human placed this project.
    out = rows(conn, """
        SELECT p.project_id projectId, p.name, p.root_path rootPath,
               p.group_id groupId, g.name groupName, p.group_pinned groupPinned,
               coalesce(sum(sp.ended_at - sp.started_at),0) activeMs,
               count(DISTINCT s.id) sessions, count(DISTINCT sp.thread_id) threads,
               min(sp.started_at) firstTs, max(sp.ended_at) lastTs
        FROM project p
        JOIN session s ON s.project_id = p.project_id
        JOIN span sp ON sp.session_id = s.id
        LEFT JOIN project_group g ON g.group_id = p.group_id
        GROUP BY 1,2,3,4,5,6 ORDER BY activeMs DESC""")
    for r in out:
        r["groupPinned"] = bool(r["groupPinned"])
    return {"projects": out}


def groups(conn):
    """One row per logical project (docs/GROUPING.md), plus the remainder.

    `ungrouped` is the exact complement of the join above, so the two halves
    add up to summary.activeMs. Before `cci group auto` has run, every project
    is ungrouped and `groups` is empty -- the normal starting state.
    """
    return {
        "groups": rows(conn, """
            SELECT g.group_id groupId, g.name, g.origin, g.forge, g.owner, g.repo,
                   g.web_url webUrl,
                   coalesce(sum(sp.ended_at - sp.started_at),0) activeMs,
                   count(DISTINCT sp.session_id) sessions,
                   count(DISTINCT sp.thread_id) threads,
                   count(DISTINCT p.project_id) projects,
                   count(DISTINCT CASE WHEN p.group_pinned = 1 THEN p.project_id END)
                       pinnedProjects,
                   min(sp.started_at) firstTs, max(sp.ended_at) lastTs
            FROM project_group g
            JOIN project p ON p.group_id = g.group_id
            JOIN session s ON s.project_id = p.project_id
            JOIN span sp ON sp.session_id = s.id
            GROUP BY 1,2,3,4,5,6,7 ORDER BY activeMs DESC, g.name"""),
        "ungrouped": one(conn, """
            SELECT count(DISTINCT s.project_id) projects,
                   coalesce(sum(sp.ended_at - sp.started_at),0) activeMs
            FROM span sp JOIN session s ON s.id = sp.session_id
            WHERE NOT EXISTS (SELECT 1 FROM project p
                              JOIN project_group g ON g.group_id = p.group_id
                              WHERE p.project_id = s.project_id)"""),
    }


def agents(conn):
    return {"agents": rows(conn, """
        SELECT t.agent_name agentName, s.source, count(DISTINCT t.id) threads,
               coalesce(sum(sp.ended_at - sp.started_at),0) activeMs
        FROM thread t JOIN session s ON s.id = t.session_id
        JOIN span sp ON sp.thread_id = t.id
        WHERE t.agent_name IS NOT NULL GROUP BY 1,2 ORDER BY activeMs DESC""")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=Path("frontend/src/fixtures"))
    a = ap.parse_args()

    cfg = config_mod.load(a.config_dir, create=False)
    conn = db.connect(cfg.db_path)
    a.out.mkdir(parents=True, exist_ok=True)

    for name, fn in (("meta", lambda c: meta(c, cfg)), ("summary", summary),
                     ("timeline", timeline), ("daily", daily), ("concurrency", concurrency),
                     ("projects", projects), ("groups", groups), ("agents", agents),
                     ("heatmap", heatmap)):
        data = fn(conn)
        p = a.out / f"{name}.json"
        p.write_text(json.dumps(data, indent=1))
        print(f"  {name + '.json':<18} {p.stat().st_size / 1024:>8.1f} KB")


if __name__ == "__main__":
    main()
