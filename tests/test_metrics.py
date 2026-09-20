"""Tests for the filter-aware metrics layer.

Two kinds of test live here.

**Synthetic corpus.** A small hand-built database, constructed from *local*
wall-clock times so that a day- or hour-boundary assertion is true in every
timezone rather than only in the author's. It is small enough that every
expected millisecond is written out longhand: a test that recomputes the
implementation's arithmetic proves nothing.

**Real corpus** (gated on `CC_INSIGHTS_REAL_CORPUS=1`). The captured fixtures
in `frontend/src/fixtures` cannot be compared against as frozen numbers -- the
corpus grows while the suite runs (docs/FINDINGS.md §0b). So the real-corpus
test loads `scripts/dump_fixtures.py`, the implementation that produced those
fixtures, and compares it against this module **in one process against one
connection**, which is exact and stable. Field names and shapes are checked
against the committed files separately, and unconditionally, because those
cannot drift.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from cc_insights import config as config_mod, cost as cost_mod, db, derive, metrics
from cc_insights.config import Config
from cc_insights.metrics import FilterError, Filters
from cc_insights.sources.base import EventKind

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "frontend" / "src" / "fixtures"

REAL_CORPUS = os.environ.get("CC_INSIGHTS_REAL_CORPUS") == "1"
_real_only = pytest.mark.skipif(
    not REAL_CORPUS,
    reason="set CC_INSIGHTS_REAL_CORPUS=1 to build a database from ~/.claude and ~/.codex",
)

MINUTE = 60_000


def at(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> int:
    """Epoch ms for a *local* wall-clock instant.

    The day and hour series bucket by local time, so the expectations below
    have to be anchored to local time too, or this file would only pass in one
    timezone. Mid-May is chosen because no common zone shifts its clocks then.
    """
    return int(datetime(y, mo, d, h, mi, s).timestamp() * 1000)


# --------------------------------------------------------------------------
# a small synthetic corpus
# --------------------------------------------------------------------------
# Laid out so that every list the contract defines has at least one row, and
# every branch that matters has a witness:
#
#   alpha / claude_code   T1 root, attended     09:00 -> 09:05   300_000 ms
#                         T2 subagent           09:01 -> 09:03   120_000 ms
#                             ... overlapping, so concurrency reaches 2
#   beta  / codex         T3 root, attended NULL  May 10 23:58 -> May 11 00:01
#                             ... 180_000 ms split 120_000 / 60_000 over midnight
#                         T4 subagent, named     May 11 10:00 -> 10:02  120_000 ms
#   gamma / claude_code   T5 root, ONE event -> no span at all
#
# T5 exists to make "reachable from the surviving spans" observable: its
# session, thread and event are in the database and in no span.

A_START, A_END = at(2026, 5, 10, 9, 0), at(2026, 5, 10, 9, 5)
B_START, B_END = at(2026, 5, 10, 9, 1), at(2026, 5, 10, 9, 3)
C_START, C_END = at(2026, 5, 10, 23, 58), at(2026, 5, 11, 0, 1)
D_START, D_END = at(2026, 5, 11, 10, 0), at(2026, 5, 11, 10, 2)

ALPHA_MS = 300_000          # T1
SUB_MS = 120_000            # T2
MIDNIGHT_MS = 180_000       # T3, 120_000 on the 10th + 60_000 on the 11th
CODEX_SUB_MS = 120_000      # T4
TOTAL_MS = ALPHA_MS + SUB_MS + MIDNIGHT_MS + CODEX_SUB_MS

# Grouping (docs/GROUPING.md), laid over the same corpus:
#
#   g-omega   alpha (pinned by a human) + gamma (a path with no spans)
#   g-void    a group no project points at -- a roster entry with no time
#   p-beta    deliberately ungrouped
#
# So the group holds alpha's time, the ungrouped remainder is beta's, and the
# two must add up to TOTAL_MS. gamma is in the group and contributes nothing,
# which is what makes "projects counts the members the spans reach" testable.
OMEGA_MS = ALPHA_MS + SUB_MS        # 420_000
UNGROUPED_MS = MIDNIGHT_MS + CODEX_SUB_MS   # 300_000


def _insert(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES ('h1', 'test-host', 'Test 1.0', ?, ?)", (A_START, D_END))
    for pid, name, root in (
        ("p-alpha", "alpha", "/code/alpha"),
        ("p-beta", "beta", "/code/beta"),
        ("p-gamma", "gamma", "/code/gamma"),
    ):
        conn.execute("INSERT INTO project (project_id, root_path, name) VALUES (?, ?, ?)",
                     (pid, root, name))

    for gid, name, origin, forge, owner, repo, web in (
        ("g-omega", "omega", "git_remote", "github", "acme", "omega",
         "https://github.com/acme/omega"),
        ("g-void", "void", "manual", None, None, None, None),
    ):
        conn.execute(
            "INSERT INTO project_group (group_id, name, origin, match_key, remote_url,"
            " forge, owner, repo, web_url, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (gid, name, origin, gid, web, forge, owner, repo, web, A_START, A_START))
    # alpha was placed by a human; gamma was detected. beta stays ungrouped.
    conn.execute("UPDATE project SET group_id = 'g-omega', group_pinned = 1"
                 " WHERE project_id = 'p-alpha'")
    conn.execute("UPDATE project SET group_id = 'g-omega' WHERE project_id = 'p-gamma'")

    for sid, source, pid, lo, hi in (
        ("s1", "claude_code", "p-alpha", A_START, A_END),
        ("s2", "codex", "p-beta", C_START, D_END),
        ("s3", "claude_code", "p-gamma", D_START, D_START),
    ):
        conn.execute(
            "INSERT INTO session (id, native_id, source, host_id, project_id, cwd,"
            " git_branch, cli_version, started_at, ended_at, event_count, active_ms)"
            " VALUES (?, ?, ?, 'h1', ?, '/tmp', 'main', '1.0', ?, ?, 0, 0)",
            (sid, f"native-{sid}", source, pid, lo, hi))

    for tid, sess, parent, sub, agent, lo, hi in (
        ("t1", "s1", None, 0, None, A_START, A_END),
        ("t2", "s1", "t1", 1, "general-purpose", B_START, B_END),
        ("t3", "s2", None, 0, None, C_START, C_END),
        ("t4", "s2", "t3", 1, "Confucius", D_START, D_END),
        ("t5", "s3", None, 0, None, D_START, D_START),
    ):
        conn.execute(
            "INSERT INTO thread (id, native_id, session_id, parent_thread_id, is_subagent,"
            " agent_name, started_at, ended_at, event_count, active_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (tid, f"native-{tid}", sess, parent, sub, agent, lo, hi))

    # (thread, ts, kind, model, in, out, cache_read, cache_write)
    events = [
        ("t1", A_START, EventKind.USER_PROMPT, None, 0, 0, 0, 0),
        ("t1", A_START + 2 * MINUTE, EventKind.ASSISTANT, "opus", 10, 20, 30, 40),
        ("t1", A_END, EventKind.ASSISTANT, "opus", 1, 2, 3, 4),
        ("t2", B_START, EventKind.USER_PROMPT, None, 0, 0, 0, 0),
        ("t2", B_END, EventKind.ASSISTANT, "sonnet", 100, 200, 300, 400),
        ("t3", C_START, EventKind.ASSISTANT, "gpt", 5, 6, 7, 8),
        ("t3", C_END, EventKind.ASSISTANT, "gpt", 5, 6, 7, 8),
        ("t4", D_START, EventKind.USER_PROMPT, None, 0, 0, 0, 0),
        ("t4", D_END, EventKind.ASSISTANT, "gpt", 1, 1, 1, 1),
        # The lone event: one timestamp is an instant, not an interval, so
        # derive gives thread t5 no span and nothing below can reach it.
        ("t5", D_START, EventKind.USER_PROMPT, None, 9_999, 0, 0, 0),
    ]
    for n, (tid, ts, kind, model, i, o, cr, cw) in enumerate(events):
        conn.execute(
            "INSERT INTO event (id, session_id, thread_id, native_event_id, ts, ordinal,"
            " kind, model, tool_name, tool_use_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
            (f"e{n}", {"t1": "s1", "t2": "s1", "t3": "s2", "t4": "s2", "t5": "s3"}[tid],
             tid, f"native-e{n}", ts, n, str(kind), model, i, o, cr, cw))


# Prices, arranged so every way cost can be incomplete has a witness:
#
#   opus    fully priced
#   sonnet  priced, but as a NEIGHBOUR (matched_id names another model) and
#           with no cache-write rate -- the two ways a rate can be partial
#   gpt     no rate at all
#
# Which makes `approximations`, `unpricedModels` and both `unpriced` reasons
# non-empty in the synthetic corpus, so a rename in any of them fails here
# rather than in someone's browser.
def _insert_prices(conn: sqlite3.Connection) -> None:
    for model, rates, matched in (
        ("opus", (15.0, 75.0, 1.5, 18.75), "anthropic/opus"),
        ("sonnet", (3.0, 15.0, 0.3, None), "anthropic/sonnet-neighbour"),
    ):
        conn.execute(
            "INSERT INTO model_price (model, effective_from, input_mtok, output_mtok,"
            " cache_read_mtok, cache_write_mtok, currency, origin, matched_id, note,"
            " updated_at) VALUES (?, 0, ?, ?, ?, ?, 'USD', 'genai-prices', ?, NULL, ?)",
            (model, *rates, matched, A_START))


@pytest.fixture
def small(conn: sqlite3.Connection, tmp_path: Path):
    """The synthetic corpus, with spans and costs from the real engines."""
    _insert(conn)
    _insert_prices(conn)
    derive.derive(conn, idle_threshold_s=300)
    cost_mod.derive_costs(conn)
    cfg = Config(host_id="h1", hostname="test-host", db_path=tmp_path / "test.db",
                 idle_threshold_s=300, config_dir=tmp_path)
    return conn, cfg


# --------------------------------------------------------------------------
# filter parsing
# --------------------------------------------------------------------------
def test_from_query_reads_every_documented_parameter():
    f = Filters.from_query({
        "project": ["a", "b"], "group": ["g1", "g2"], "source": ["codex"],
        "from": ["1000"], "to": ["2000"], "role": ["root"],
    })
    assert f == Filters(projects=["a", "b"], groups=["g1", "g2"], sources=["codex"],
                        ts_from=1000, ts_to=2000, role="root")


def test_from_query_defaults_to_no_filter_at_all():
    f = Filters.from_query({})
    assert f == Filters() and f.role == "all" and f.is_empty


@pytest.mark.parametrize("params", [
    {"project": [""]}, {"group": [""]}, {"source": [""]}, {"from": [""]}, {"to": [""]},
])
def test_blank_values_are_not_a_filter(params):
    assert Filters.from_query(params).is_empty


def test_unknown_parameters_are_ignored_not_rejected():
    # A cache-buster or a UI's own state in the URL is not a malformed filter.
    assert Filters.from_query({"_": ["17"], "zoom": ["day"]}).is_empty


def test_repeated_scalar_takes_the_last_value():
    assert Filters.from_query({"from": ["1", "2"]}).ts_from == 2


@pytest.mark.parametrize("params", [
    {"from": ["yesterday"]}, {"to": ["12.5"]}, {"from": ["0x10"]},
])
def test_unparseable_timestamp_is_a_filter_error(params):
    with pytest.raises(FilterError):
        Filters.from_query(params)


def test_unknown_role_is_a_filter_error():
    with pytest.raises(FilterError):
        Filters.from_query({"role": ["human"]})
    with pytest.raises(FilterError):
        Filters(role="human")  # type: ignore[arg-type]


def test_negative_epoch_is_accepted():
    # Pre-1970 is nonsense for this corpus but it is not malformed, and
    # guessing at plausibility is how a filter starts silently dropping data.
    assert Filters.from_query({"from": ["-1"]}).ts_from == -1


# --------------------------------------------------------------------------
# shape: the wire contract
# --------------------------------------------------------------------------
# Two objects in the contract are maps keyed by *data* -- a source name, a
# concurrency level -- so their keys are values, not field names.
_OPEN_MAPS = {"bySource", "timeAtLevel"}


def _shape(value, key: str | None = None):
    """Keys and leaf types, recursively; lists collapse to their first item."""
    if isinstance(value, dict):
        if key in _OPEN_MAPS:
            return {"<map>": sorted({_shape(v) for v in value.values()})}
        return {k: _shape(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0], key)] if value else []
    return type(value).__name__


def _dropped(expected, actual, path: str = "") -> list[str]:
    """Every leaf of `expected` that `actual` no longer carries, by path."""
    here = path or "<root>"
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{here}: was an object, is {actual}"]
        out: list[str] = []
        for k, v in expected.items():
            sub = f"{path}.{k}".lstrip(".")
            out += [sub] if k not in actual else _dropped(v, actual[k], sub)
        return out
    if isinstance(expected, list):
        if not expected:
            return []
        if not isinstance(actual, list) or not actual:
            return [f"{here}[]: a populated list became {actual}"]
        return _dropped(expected[0], actual[0], f"{path}[]")
    # A nullable field is null in one sample and populated in another purely
    # because of DB state -- `pathExists` is null until `cci group auto` has
    # probed, `groupId` until projects are grouped. That is the contract
    # working, not a field changing type, so NoneType is compatible either way.
    # A field genuinely disappearing, or turning from str into int, still fails.
    if "NoneType" in (str(expected), str(actual)):
        return []
    return [] if expected == actual else [f"{here}: {expected} became {actual}"]


@pytest.mark.parametrize("name", metrics.ENDPOINTS)
def test_output_shape_still_carries_every_committed_field(small, name):
    """Field names and types are the frozen half of the contract.

    Numbers drift with the corpus; these do not. The synthetic database is
    built to populate every list, so an empty one here would mean a key the
    frontend reads was renamed or dropped.

    The committed fixtures predate grouping, so this is a **superset** check:
    every field the frontend already reads must still be there with the same
    type, and a new one is allowed. Renaming or dropping a field still fails.
    The fields grouping added are pinned by name in the group tests below, and
    `groups.json` appears here once the fixtures are regenerated.
    """
    conn, cfg = small
    fixture = FIXTURES / f"{name}.json"
    if not fixture.exists():
        pytest.skip(f"{name}.json is captured when the fixtures are regenerated")
    mine = metrics.endpoint(name, conn, Filters(), cfg)
    captured = json.loads(fixture.read_text())
    assert _dropped(_shape(captured), _shape(mine)) == []


def test_every_contract_endpoint_is_implemented():
    assert metrics.ENDPOINTS == ("meta", "summary", "timeline", "daily", "concurrency",
                                 "projects", "groups", "agents", "heatmap", "cost")
    captured = {p.name.removesuffix(".json") for p in FIXTURES.glob("*.json")}
    # One fixture per endpoint, both ways: a fixture with no endpoint behind it
    # is dead weight the frontend may still be reading, and an endpoint with no
    # fixture is one the frontend cannot be built against offline.
    assert captured == set(metrics.ENDPOINTS)


def test_unknown_endpoint_name_raises():
    with pytest.raises(KeyError):
        metrics.endpoint("nope", None, Filters(), None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# unfiltered numbers on the synthetic corpus
# --------------------------------------------------------------------------
def test_summary_totals(small):
    conn, _ = small
    s = metrics.summary(conn, Filters())
    assert s["spans"] == 4
    assert s["activeMs"] == TOTAL_MS == 720_000
    assert s["bySource"] == [
        {"source": "claude_code", "activeMs": ALPHA_MS + SUB_MS},
        {"source": "codex", "activeMs": MIDNIGHT_MS + CODEX_SUB_MS},
    ]


def test_the_three_buckets_partition_active_time(small):
    conn, _ = small
    s = metrics.summary(conn, Filters())
    assert s["humanInitiatedMs"] == ALPHA_MS            # t1: root, human turn
    assert s["autonomousMs"] == SUB_MS + CODEX_SUB_MS   # t2, t4: subagents
    assert s["unattendedRootMs"] == MIDNIGHT_MS         # t3: root, attended NULL
    assert (s["humanInitiatedMs"] + s["autonomousMs"] + s["unattendedRootMs"]
            == s["activeMs"])


def test_counts_are_reachable_from_spans_not_global(small):
    """The contract: a filter narrows spans, and counts follow the spans.

    Thread t5 has a single event, so derive gives it no span. It is a real row
    in every table and is reachable from none of them -- which is why these
    counts are one short of `SELECT count(*)`.
    """
    conn, _ = small
    s = metrics.summary(conn, Filters())
    assert s["sessions"] == 2 and conn.execute(
        "SELECT count(*) FROM session").fetchone()[0] == 3
    assert s["threads"] == 4 and conn.execute(
        "SELECT count(*) FROM thread").fetchone()[0] == 5
    assert s["events"] == 9 and conn.execute(
        "SELECT count(*) FROM event").fetchone()[0] == 10
    # ... and the lone event's tokens are not counted either.
    assert s["tokens"]["input"] == 10 + 1 + 100 + 5 + 5 + 1


def test_meta_is_the_roster_and_projects_is_the_ranking(small):
    """`meta` offers every project as a filter choice; `projects` ranks the
    ones with time in range. gamma has a session and no span."""
    conn, cfg = small
    m = metrics.meta(conn, cfg)
    assert [p["name"] for p in m["projects"]] == ["alpha", "beta", "gamma"]
    assert m["projects"][-1] == {"projectId": "p-gamma", "name": "gamma",
                                 "rootPath": "/code/gamma", "activeMs": 0}
    assert m["sources"] == ["claude_code", "codex"]
    assert m["agents"] == [{"agentName": "Confucius", "source": "codex"},
                           {"agentName": "general-purpose", "source": "claude_code"}]
    assert m["models"] == ["gpt", "opus", "sonnet"]
    assert m["idleThresholdS"] == 300
    assert m["hostname"] == "test-host"
    assert m["firstTs"] == A_START and m["lastTs"] == D_END

    # gamma is absent: `projects` ranks time, and gamma has none.
    assert [p["name"] for p in metrics.projects(conn, Filters())["projects"]] == [
        "alpha", "beta"]


def test_timeline_rows_carry_the_contract_fields(small):
    conn, _ = small
    t = metrics.timeline(conn, Filters())
    assert t["truncated"] is False and t["limit"] == metrics.TIMELINE_LIMIT
    assert [r["start"] for r in t["spans"]] == sorted(r["start"] for r in t["spans"])
    row = next(r for r in t["spans"] if r["threadId"] == "t2")
    # Exhaustive on purpose: a span is what the swimlane draws, so a field
    # silently appearing or vanishing here changes the picture.
    assert row == {"spanId": row["spanId"], "threadId": "t2", "sessionId": "s1",
                   "projectId": "p-alpha", "projectName": "alpha",
                   # The logical project. A lane is labelled by this, not by
                   # projectName -- the path `retry-budget-spike` belongs to the
                   # project atlas-chat, and labelling by path splits one
                   # project across several lanes.
                   "groupId": "g-omega", "groupName": "omega",
                   "source": "claude_code", "agentName": "general-purpose",
                   "isSubagent": True, "parentThreadId": "t1", "attended": 0,
                   "start": B_START, "end": B_END}
    # attended NULL survives to the wire as null, not as 0.
    assert next(r for r in t["spans"] if r["threadId"] == "t3")["attended"] is None


def test_timeline_truncates_to_the_widest_spans(small, monkeypatch):
    conn, _ = small
    monkeypatch.setattr(metrics, "TIMELINE_LIMIT", 2)
    t = metrics.timeline(conn, Filters())
    assert t["truncated"] is True and t["limit"] == 2 and len(t["spans"]) == 2
    assert {r["threadId"] for r in t["spans"]} == {"t1", "t3"}   # 300k and 180k
    assert [r["start"] for r in t["spans"]] == sorted(r["start"] for r in t["spans"])


def test_concurrency_counts_the_overlap(small):
    conn, _ = small
    c = metrics.concurrency(conn, Filters())
    # t1 and t2 overlap for two minutes; nothing else overlaps anything.
    assert c["timeAtLevel"] == {"1": ALPHA_MS - SUB_MS + MIDNIGHT_MS + CODEX_SUB_MS,
                                "2": SUB_MS}
    assert c["peak"] == 2 and c["peakAt"] == B_START
    assert c["wallMs"] == ALPHA_MS + MIDNIGHT_MS + CODEX_SUB_MS
    assert c["activeMs"] == TOTAL_MS
    assert c["multiplier"] == round(TOTAL_MS / c["wallMs"], 4)


def test_agents_group_by_name_and_source(small):
    conn, _ = small
    # Both agents hold the same time here, so order between them is a tie the
    # contract does not resolve; the rows are what matters.
    assert sorted(metrics.agents(conn, Filters())["agents"], key=lambda a: a["agentName"]) == [
        {"agentName": "Confucius", "source": "codex", "threads": 1, "activeMs": CODEX_SUB_MS},
        {"agentName": "general-purpose", "source": "claude_code",
         "threads": 1, "activeMs": SUB_MS},
    ]


# --------------------------------------------------------------------------
# day and hour boundaries
# --------------------------------------------------------------------------
def test_a_span_crossing_midnight_is_split_between_the_two_days(small):
    """23:58 -> 00:01 is two minutes of one day and one of the next.

    Charging the whole span to its start day is the failure this splits guard
    against: it would silently move every overnight run into the evening.
    """
    conn, _ = small
    days = {d["date"]: d for d in metrics.daily(conn, Filters())["days"]}
    assert days["2026-05-10"]["bySource"]["codex"] == 2 * MINUTE
    assert days["2026-05-11"]["bySource"]["codex"] == 1 * MINUTE + CODEX_SUB_MS
    assert days["2026-05-10"]["activeMs"] == ALPHA_MS + SUB_MS + 2 * MINUTE
    assert days["2026-05-11"]["activeMs"] == 1 * MINUTE + CODEX_SUB_MS
    assert sum(d["activeMs"] for d in days.values()) == TOTAL_MS


def test_wall_ms_is_the_union_of_the_days_spans_not_their_sum(small):
    """The 10th holds two overlapping spans; the 11th holds two disjoint ones.

    09:00->09:05 and 09:01->09:03 are five minutes of clock and seven minutes
    of work, so that day's `wallMs` must be the five. Copying `activeMs` into
    `wallMs` -- which this did until the definition was fixed -- reports every
    day as perfectly serial and makes parallelism invisible.
    """
    conn, _ = small
    days = {d["date"]: d for d in metrics.daily(conn, Filters())["days"]}

    tenth = days["2026-05-10"]
    assert tenth["activeMs"] == ALPHA_MS + SUB_MS + 2 * MINUTE     # 540_000
    assert tenth["wallMs"] == ALPHA_MS + 2 * MINUTE                # 420_000
    assert tenth["wallMs"] < tenth["activeMs"]

    # Nothing overlaps on the 11th, so there the two agree exactly.
    eleventh = days["2026-05-11"]
    assert eleventh["activeMs"] == eleventh["wallMs"] == 1 * MINUTE + CODEX_SUB_MS


def test_wall_ms_never_exceeds_active_ms_and_follows_the_filter(small):
    conn, _ = small
    for f in (Filters(), Filters(role="root"), Filters(role="subagent"),
              Filters(sources=["codex"]), Filters(projects=["p-alpha"])):
        for day in metrics.daily(conn, f)["days"]:
            assert 0 <= day["wallMs"] <= day["activeMs"], (f, day)

    # role=root drops the overlapping subagent, so the day becomes serial.
    root_tenth = next(d for d in metrics.daily(conn, Filters(role="root"))["days"]
                      if d["date"] == "2026-05-10")
    assert root_tenth["activeMs"] == root_tenth["wallMs"] == ALPHA_MS + 2 * MINUTE


def test_wall_ms_clips_to_the_local_day(small):
    """A span that runs past midnight lends each day only its own piece."""
    conn, _ = small
    conn.execute("DELETE FROM span WHERE thread_id IN ('t1', 't2', 't4')")
    days = {d["date"]: d["wallMs"] for d in metrics.daily(conn, Filters())["days"]}
    assert days == {"2026-05-10": 2 * MINUTE, "2026-05-11": 1 * MINUTE}


@pytest.mark.parametrize("intervals, expected", [
    ([], 0),
    ([(0, 10)], 10),
    ([(0, 10), (20, 30)], 20),               # disjoint
    ([(0, 10), (5, 15)], 15),                # overlapping
    ([(0, 30), (5, 10)], 30),                # contained
    ([(0, 10), (10, 20)], 20),               # touching
    ([(20, 30), (0, 10), (5, 25)], 30),      # unsorted, chained
    ([(5, 5), (0, 10)], 10),                 # zero-length
])
def test_union_ms(intervals, expected):
    assert metrics._union_ms(intervals) == expected


def test_daily_fills_the_gap_days_with_zero(small):
    conn, _ = small
    days = metrics.daily(conn, Filters(ts_from=at(2026, 5, 10, 0, 0)))["days"]
    assert [d["date"] for d in days] == ["2026-05-10", "2026-05-11"]
    # A range with a hole keeps the hole visible rather than closing it up.
    conn.execute("UPDATE span SET started_at = started_at + 4 * 86400000,"
                 " ended_at = ended_at + 4 * 86400000 WHERE thread_id = 't4'")
    days = metrics.daily(conn, Filters())["days"]
    assert [d["date"] for d in days] == ["2026-05-10", "2026-05-11", "2026-05-12",
                                         "2026-05-13", "2026-05-14", "2026-05-15"]
    assert [d["activeMs"] for d in days[2:5]] == [0, 0, 0]
    assert all(d["bySource"] == {} for d in days[2:5])


def test_a_span_crossing_an_hour_is_split_between_the_two_cells(small):
    conn, _ = small
    cells = {(c["weekday"], c["hour"]): c["activeMs"]
             for c in metrics.heatmap(conn, Filters())["cells"]}
    # 2026-05-10 is a Sunday (weekday 6); 2026-05-11 a Monday (weekday 0).
    assert cells[(6, 23)] == 2 * MINUTE
    assert cells[(0, 0)] == 1 * MINUTE
    assert cells[(6, 9)] == ALPHA_MS + SUB_MS
    assert cells[(0, 10)] == CODEX_SUB_MS
    assert sum(cells.values()) == TOTAL_MS


def test_heatmap_weekday_zero_is_monday(small):
    conn, _ = small
    cells = metrics.heatmap(conn, Filters())["cells"]
    assert all(0 <= c["weekday"] <= 6 and 0 <= c["hour"] <= 23 for c in cells)
    assert datetime(2026, 5, 11).weekday() == 0   # the Monday the cell above used


# --------------------------------------------------------------------------
# filters actually filter
# --------------------------------------------------------------------------
def test_a_project_filter_is_a_strict_subset_with_less_active_time(small):
    conn, _ = small
    everything = metrics.summary(conn, Filters())
    alpha = metrics.summary(conn, Filters(projects=["p-alpha"]))

    assert alpha["activeMs"] < everything["activeMs"]
    assert alpha["activeMs"] == ALPHA_MS + SUB_MS
    assert alpha["sessions"] == 1 and alpha["threads"] == 2 and alpha["spans"] == 2
    assert alpha["bySource"] == [{"source": "claude_code", "activeMs": ALPHA_MS + SUB_MS}]

    ids = lambda f: {r["spanId"] for r in metrics.timeline(conn, f)["spans"]}
    assert ids(Filters(projects=["p-alpha"])) < ids(Filters())
    assert [p["projectId"] for p in
            metrics.projects(conn, Filters(projects=["p-alpha"]))["projects"]] == ["p-alpha"]
    assert metrics.agents(conn, Filters(projects=["p-alpha"]))["agents"] == [
        {"agentName": "general-purpose", "source": "claude_code",
         "threads": 1, "activeMs": SUB_MS}]


def test_several_projects_union(small):
    conn, _ = small
    both = metrics.summary(conn, Filters(projects=["p-alpha", "p-beta"]))
    assert both["activeMs"] == TOTAL_MS
    assert both["sessions"] == 2


# --------------------------------------------------------------------------
# grouping: the `group` filter and /api/groups
# --------------------------------------------------------------------------
def test_a_group_filter_is_exactly_the_union_of_its_member_projects(small):
    """The whole point: g-omega is alpha + gamma, so it must read as both.

    Compared as whole payloads rather than as one number, because the two
    filters must select the same *spans*, not merely the same total.
    """
    conn, _ = small
    by_group = metrics.summary(conn, Filters(groups=["g-omega"]))
    by_members = metrics.summary(conn, Filters(projects=["p-alpha", "p-gamma"]))
    assert by_group == by_members
    assert by_group["activeMs"] == OMEGA_MS == 420_000
    assert by_group["activeMs"] < metrics.summary(conn, Filters())["activeMs"]

    ids = lambda f: {r["spanId"] for r in metrics.timeline(conn, f)["spans"]}
    assert ids(Filters(groups=["g-omega"])) == ids(Filters(projects=["p-alpha", "p-gamma"]))
    assert ids(Filters(groups=["g-omega"])) < ids(Filters())


def test_group_and_project_combine_as_a_union_not_an_intersection(small):
    """`?group=G&project=P` is "G plus P", never "P if P is in G".

    The two sets here are disjoint, so an intersection would have answered an
    empty dashboard -- which is exactly the mistake this pins down.
    """
    conn, _ = small
    ids = lambda **kw: {r["spanId"] for r in metrics.timeline(conn, Filters(**kw))["spans"]}
    group_only = ids(groups=["g-omega"])
    project_only = ids(projects=["p-beta"])
    assert group_only and project_only
    assert not (group_only & project_only), "the fixture must make the union visible"

    assert ids(groups=["g-omega"], projects=["p-beta"]) == group_only | project_only == ids()
    s = metrics.summary(conn, Filters(groups=["g-omega"], projects=["p-beta"]))
    assert s["activeMs"] == OMEGA_MS + UNGROUPED_MS == TOTAL_MS
    assert s["sessions"] == 2


def test_a_project_already_inside_the_filtered_group_is_not_counted_twice(small):
    conn, _ = small
    s = metrics.summary(conn, Filters(groups=["g-omega"], projects=["p-alpha"]))
    assert s["activeMs"] == OMEGA_MS
    assert s["spans"] == 2


def test_the_group_project_union_still_intersects_with_every_other_filter(small):
    """(G or P) AND source AND role AND range -- the union binds tighter."""
    conn, _ = small
    both = dict(groups=["g-omega"], projects=["p-beta"])
    assert metrics.summary(conn, Filters(**both, sources=["codex"]))["activeMs"] == UNGROUPED_MS
    assert metrics.summary(conn, Filters(**both, role="subagent"))["activeMs"] == (
        SUB_MS + CODEX_SUB_MS)
    assert metrics.summary(conn, Filters(**both, ts_from=at(2026, 5, 11, 0, 0)))[
        "activeMs"] == CODEX_SUB_MS


def test_an_unknown_group_matches_nothing_rather_than_erroring(small):
    conn, _ = small
    assert metrics.summary(conn, Filters(groups=["g-nope"]))["activeMs"] == 0
    assert metrics.groups(conn, Filters(groups=["g-nope"])) == {
        "groups": [], "ungrouped": {"projects": 0, "activeMs": 0}}

    hostile = "g-omega'; DROP TABLE span; --"
    assert metrics.summary(conn, Filters(groups=[hostile]))["activeMs"] == 0
    assert conn.execute("SELECT count(*) FROM span").fetchone()[0] == 4


def test_a_group_filter_narrows_every_endpoint_not_just_summary(small):
    conn, _ = small
    f = Filters(groups=["g-omega"])

    assert metrics.summary(conn, f)["activeMs"] == OMEGA_MS
    assert {r["projectId"] for r in metrics.timeline(conn, f)["spans"]} == {"p-alpha"}

    days = metrics.daily(conn, f)["days"]
    assert [d["date"] for d in days] == ["2026-05-10"]      # beta's days are gone
    assert sum(d["activeMs"] for d in days) == OMEGA_MS
    assert days[0]["wallMs"] == ALPHA_MS                     # the subagent overlaps

    c = metrics.concurrency(conn, f)
    assert c["activeMs"] == OMEGA_MS and c["peak"] == 2

    assert [p["projectId"] for p in metrics.projects(conn, f)["projects"]] == ["p-alpha"]
    assert metrics.agents(conn, f)["agents"] == [
        {"agentName": "general-purpose", "source": "claude_code",
         "threads": 1, "activeMs": SUB_MS}]
    assert sum(cell["activeMs"] for cell in metrics.heatmap(conn, f)["cells"]) == OMEGA_MS

    g = metrics.groups(conn, f)
    assert [row["groupId"] for row in g["groups"]] == ["g-omega"]
    assert g["ungrouped"] == {"projects": 0, "activeMs": 0}


def test_groups_rows_carry_the_contract_fields(small):
    conn, _ = small
    g = metrics.groups(conn, Filters())
    assert g["groups"] == [{
        "groupId": "g-omega", "name": "omega", "origin": "git_remote",
        "forge": "github", "owner": "acme", "repo": "omega",
        "webUrl": "https://github.com/acme/omega",
        "activeMs": OMEGA_MS, "sessions": 1, "threads": 2,
        # gamma is a member and holds no span, so the ranking counts one
        # project; alpha was placed by a human, so one of them is pinned.
        "projects": 1, "pinnedProjects": 1,
        "firstTs": A_START, "lastTs": A_END,
    }]
    # g-void has no members at all: a roster entry, not a ranked row.
    assert "g-void" not in {row["groupId"] for row in g["groups"]}
    assert g["ungrouped"] == {"projects": 1, "activeMs": UNGROUPED_MS}


def test_groups_and_ungrouped_partition_active_time_under_every_filter(small):
    """sum(groups[].activeMs) + ungrouped.activeMs == summary.activeMs.

    The two halves are the same FROM with the join inverted, so this is an
    identity rather than two definitions that happen to agree today.
    """
    conn, _ = small
    unfiltered = metrics.groups(conn, Filters())
    assert (sum(r["activeMs"] for r in unfiltered["groups"])
            + unfiltered["ungrouped"]["activeMs"]
            == metrics.summary(conn, Filters())["activeMs"] == TOTAL_MS)

    for f in (Filters(role="root"), Filters(role="subagent"), Filters(sources=["codex"]),
              Filters(projects=["p-alpha"]), Filters(groups=["g-omega"]),
              Filters(groups=["g-omega"], projects=["p-beta"]),
              Filters(ts_from=at(2026, 5, 11, 0, 0)), Filters(projects=["nope"])):
        g = metrics.groups(conn, f)
        assert (sum(r["activeMs"] for r in g["groups"]) + g["ungrouped"]["activeMs"]
                == metrics.summary(conn, f)["activeMs"]), f


def test_a_span_whose_session_has_no_project_still_lands_in_ungrouped(small):
    """`session.project_id` is nullable, and the partition must survive it.

    Defining `ungrouped` as "project.group_id IS NULL" alone would drop this
    span from both halves and quietly break the invariant above.
    """
    conn, _ = small
    conn.execute("UPDATE session SET project_id = NULL WHERE id = 's2'")
    g = metrics.groups(conn, Filters())
    assert g["ungrouped"] == {"projects": 0, "activeMs": UNGROUPED_MS}
    assert (sum(r["activeMs"] for r in g["groups"]) + g["ungrouped"]["activeMs"]
            == metrics.summary(conn, Filters())["activeMs"] == TOTAL_MS)


def test_meta_offers_every_group_as_a_filter_choice(small):
    """A roster, like `meta.projects`: a group with no time is still a choice."""
    conn, cfg = small
    m = metrics.meta(conn, cfg)
    assert m["groups"] == [
        {"groupId": "g-omega", "name": "omega", "activeMs": OMEGA_MS},
        {"groupId": "g-void", "name": "void", "activeMs": 0},
    ]


def test_projects_rows_say_which_group_they_sit_in(small):
    conn, _ = small
    rows = {p["projectId"]: p for p in metrics.projects(conn, Filters())["projects"]}
    assert rows["p-alpha"]["groupId"] == "g-omega"
    assert rows["p-alpha"]["groupName"] == "omega"
    assert rows["p-alpha"]["groupPinned"] is True      # a human placed it
    assert rows["p-beta"]["groupId"] is None
    assert rows["p-beta"]["groupName"] is None
    assert rows["p-beta"]["groupPinned"] is False


def test_everything_works_when_no_groups_exist_at_all(small):
    """The state before `cci group auto` has ever run -- the common case.

    Nothing may 500, nothing may vanish, and the whole corpus reports as
    ungrouped rather than as missing.
    """
    conn, cfg = small
    conn.execute("UPDATE project SET group_id = NULL, group_pinned = 0")
    conn.execute("DELETE FROM project_group")

    assert metrics.meta(conn, cfg)["groups"] == []
    g = metrics.groups(conn, Filters())
    assert g["groups"] == []
    # alpha and beta; gamma holds no span, so no filtered number reaches it.
    assert g["ungrouped"] == {"projects": 2, "activeMs": TOTAL_MS}
    assert g["ungrouped"]["activeMs"] == metrics.summary(conn, Filters())["activeMs"]

    assert metrics.summary(conn, Filters(groups=["g-omega"]))["activeMs"] == 0
    assert metrics.timeline(conn, Filters(groups=["g-omega"]))["spans"] == []
    assert metrics.groups(conn, Filters(groups=["g-omega"])) == {
        "groups": [], "ungrouped": {"projects": 0, "activeMs": 0}}

    rows = metrics.projects(conn, Filters())["projects"]
    assert rows and all(r["groupId"] is None and r["groupName"] is None
                        and r["groupPinned"] is False for r in rows)


def test_role_partitions_active_time_exactly(small):
    conn, _ = small
    everything = metrics.summary(conn, Filters(role="all"))
    root = metrics.summary(conn, Filters(role="root"))
    sub = metrics.summary(conn, Filters(role="subagent"))

    assert root["activeMs"] + sub["activeMs"] == everything["activeMs"]
    assert root["activeMs"] == ALPHA_MS + MIDNIGHT_MS
    assert sub["activeMs"] == SUB_MS + CODEX_SUB_MS
    assert root["autonomousMs"] == 0
    assert sub["humanInitiatedMs"] == 0 and sub["unattendedRootMs"] == 0
    assert sub["autonomousMs"] == sub["activeMs"]
    assert metrics.agents(conn, Filters(role="root"))["agents"] == []


def test_source_filter(small):
    conn, _ = small
    s = metrics.summary(conn, Filters(sources=["codex"]))
    assert s["activeMs"] == MIDNIGHT_MS + CODEX_SUB_MS
    assert {r["source"] for r in metrics.timeline(conn, Filters(sources=["codex"]))["spans"]} \
        == {"codex"}


def test_ts_from_is_inclusive_and_ts_to_is_exclusive(small):
    conn, _ = small
    just_alpha = Filters(ts_from=A_START, ts_to=B_START)
    assert {r["threadId"] for r in metrics.timeline(conn, just_alpha)["spans"]} == {"t1"}

    # The bound lands exactly on t2's start: `from` takes it, `to` does not.
    assert metrics.summary(conn, Filters(ts_from=B_START))["activeMs"] == (
        SUB_MS + MIDNIGHT_MS + CODEX_SUB_MS)
    assert metrics.summary(conn, Filters(ts_to=B_START))["activeMs"] == ALPHA_MS


def test_the_bound_is_the_span_start_so_no_span_is_ever_clipped(small):
    """A span is in or out whole. The midnight span starts on the 10th, so a
    range ending at midnight still reports all three of its minutes."""
    conn, _ = small
    f = Filters(ts_from=at(2026, 5, 10, 12, 0), ts_to=at(2026, 5, 11, 0, 0))
    assert metrics.summary(conn, f)["activeMs"] == MIDNIGHT_MS
    days = {d["date"]: d["activeMs"] for d in metrics.daily(conn, f)["days"]}
    assert days == {"2026-05-10": 2 * MINUTE, "2026-05-11": 1 * MINUTE}


def test_filters_compose(small):
    conn, _ = small
    f = Filters(projects=["p-beta"], sources=["codex"], role="subagent",
                ts_from=at(2026, 5, 11, 0, 0))
    assert metrics.summary(conn, f)["activeMs"] == CODEX_SUB_MS


def test_a_filter_that_matches_nothing_yields_contract_shaped_emptiness(small):
    conn, cfg = small
    f = Filters(projects=["does-not-exist"])
    s = metrics.summary(conn, f)
    assert s["activeMs"] == 0 and s["sessions"] == 0 and s["spans"] == 0
    assert s["bySource"] == [] and s["tokens"]["input"] == 0
    # Cost narrows with everything else, and says so rather than omitting it.
    assert s["cost"]["total"] == 0 and s["cost"]["unpricedTokens"] == 0
    assert metrics.timeline(conn, f) == {"spans": [], "truncated": False,
                                         "limit": metrics.TIMELINE_LIMIT}
    assert metrics.daily(conn, f) == {"days": [], "currency": "USD"}
    assert metrics.heatmap(conn, f) == {"cells": []}
    assert metrics.projects(conn, f) == {"projects": [], "currency": "USD"}
    assert metrics.groups(conn, f) == {"groups": [],
                                       "ungrouped": {"projects": 0, "activeMs": 0}}
    assert metrics.agents(conn, f) == {"agents": []}
    assert metrics.concurrency(conn, f) == {
        "timeAtLevel": {}, "peak": 0, "peakAt": None,
        "wallMs": 0, "activeMs": 0, "multiplier": 0}
    # meta is unfiltered by design: the filter controls must still offer every
    # choice, or a narrowed dashboard could never be widened again.
    assert len(metrics.meta(conn, cfg)["projects"]) == 3
    assert len(metrics.meta(conn, cfg)["groups"]) == 2


def test_an_unknown_source_matches_nothing_rather_than_erroring(small):
    conn, _ = small
    assert metrics.summary(conn, Filters(sources=["emacs"]))["activeMs"] == 0


def test_filter_values_are_bound_parameters(small):
    """A value is never formatted into SQL, so this is data, not syntax."""
    conn, _ = small
    hostile = "p-alpha'; DROP TABLE span; --"
    assert metrics.summary(conn, Filters(projects=[hostile]))["activeMs"] == 0
    assert metrics.timeline(conn, Filters(sources=[hostile]))["spans"] == []
    assert conn.execute("SELECT count(*) FROM span").fetchone()[0] == 4


def test_an_empty_database_answers_every_endpoint(conn, tmp_path):
    cfg = Config(host_id="h", hostname="none", db_path=tmp_path / "x.db", config_dir=tmp_path)
    for name in metrics.ENDPOINTS:
        payload = metrics.endpoint(name, conn, Filters(), cfg)
        assert isinstance(payload, dict)
    m = metrics.meta(conn, cfg)
    assert m["hostname"] == "unknown" and m["firstTs"] is None and m["lastTs"] is None
    assert metrics.summary(conn, Filters())["activeMs"] == 0


# --------------------------------------------------------------------------
# real corpus: this module against the script that produced the fixtures
# --------------------------------------------------------------------------
def _load_dump_fixtures():
    """Import `scripts/dump_fixtures.py`, the reference for every query."""
    spec = importlib.util.spec_from_file_location(
        "cci_dump_fixtures", REPO / "scripts" / "dump_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def real(tmp_path_factory):
    """A database built from this machine's real logs, once per session.

    `CC_INSIGHTS_TEST_CONFIG_DIR` points at an existing config dir to reuse its
    database instead of spending ~15s re-ingesting.
    """
    from cc_insights import ingest

    existing = os.environ.get("CC_INSIGHTS_TEST_CONFIG_DIR")
    if existing:
        cfg = config_mod.load(Path(existing), create=False)
        conn = db.connect(cfg.db_path)
        # A database ingested before a migration landed is otherwise missing
        # the tables these tests read. This is what `cci init` does to it
        # anyway, it is idempotent, and it never touches a row.
        db.migrate(conn)
        return conn, cfg

    config_dir = tmp_path_factory.mktemp("real-corpus")
    cfg = config_mod.load(config_dir, create=True)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    db.upsert_host(conn, cfg.host_id, cfg.hostname, config_mod.host_os())
    ingest.ingest(conn, cfg)
    derive.derive(conn, idle_threshold_s=cfg.idle_threshold_s)
    return conn, cfg


@_real_only
@pytest.mark.parametrize("name", metrics.ENDPOINTS)
def test_unfiltered_output_equals_the_fixture_generator(real, name):
    """One process, one connection, both implementations -- exact and stable.

    Whole-payload equality for all eight, with only `generatedAt` dropped. A
    frozen number would be stale before the run finished; this is not.
    """
    conn, cfg = real
    reference = _load_dump_fixtures()
    mine = metrics.endpoint(name, conn, Filters(), cfg)
    ref = reference.meta(conn, cfg) if name == "meta" else getattr(reference, name)(conn)
    if name == "meta":
        mine.pop("generatedAt"), ref.pop("generatedAt")
    assert mine == ref


@_real_only
def test_summary_counts_what_the_spans_reach(real):
    """The contract's rule, checked against SQL rather than against a peer.

    A row with no span -- a thread whose single event is an instant, and the
    session and event behind it -- is in the database and in no span, so it is
    outside every number this endpoint reports.
    """
    conn, _ = real
    mine = metrics.summary(conn, Filters())
    scalar = lambda sql: conn.execute(sql).fetchone()[0]

    assert mine["sessions"] == scalar("SELECT count(DISTINCT session_id) FROM span")
    assert mine["threads"] == scalar("SELECT count(DISTINCT thread_id) FROM span")
    assert mine["events"] == scalar("SELECT coalesce(sum(event_count), 0) FROM span")
    assert mine["spans"] == scalar("SELECT count(*) FROM span")

    for table, reachable in (("session", mine["sessions"]), ("thread", mine["threads"])):
        spanless = scalar(
            f"SELECT count(*) FROM {table} x WHERE NOT EXISTS (SELECT 1 FROM span sp"
            f" WHERE sp.{table}_id = x.id)")
        assert scalar(f"SELECT count(*) FROM {table}") - reachable == spanless


@_real_only
def test_real_corpus_filters_narrow_and_partition(real):
    conn, cfg = real
    everything = metrics.summary(conn, Filters())
    assert everything["activeMs"] > 0

    biggest = metrics.projects(conn, Filters())["projects"][0]["projectId"]
    one = metrics.summary(conn, Filters(projects=[biggest]))
    assert 0 < one["activeMs"] < everything["activeMs"]
    assert one["sessions"] <= everything["sessions"]
    assert {r["spanId"] for r in metrics.timeline(conn, Filters(projects=[biggest]))["spans"]} \
        < {r["spanId"] for r in metrics.timeline(conn, Filters())["spans"]}

    root = metrics.summary(conn, Filters(role="root"))["activeMs"]
    sub = metrics.summary(conn, Filters(role="subagent"))["activeMs"]
    assert root + sub == everything["activeMs"] and root > 0 and sub > 0

    meta = metrics.meta(conn, cfg)
    half = (meta["firstTs"] + meta["lastTs"]) // 2
    early = metrics.summary(conn, Filters(ts_to=half))["activeMs"]
    late = metrics.summary(conn, Filters(ts_from=half))["activeMs"]
    assert early + late == everything["activeMs"]
    assert early > 0 and late > 0


@_real_only
def test_real_days_show_parallelism_as_wall_below_active(real):
    """On the real corpus the two must differ, or `wallMs` is a copy again.

    A day where several agents ran at once has more active time than elapsed
    time; a day worked serially has exactly as much. Both must appear.
    """
    conn, _ = real
    days = metrics.daily(conn, Filters())["days"]
    assert days, "the real corpus has no days"
    for day in days:
        assert 0 <= day["wallMs"] <= day["activeMs"], day

    parallel = [d for d in days if d["wallMs"] < d["activeMs"]]
    assert parallel, "no day shows parallelism -- wallMs is not a union"
    # The widest day is the headline number this endpoint exists to produce.
    worst = max(parallel, key=lambda d: d["activeMs"] - d["wallMs"])
    assert worst["activeMs"] / worst["wallMs"] > 1.5

    # A quiet day, worked one thread at a time, still reports the two as equal.
    assert any(d["wallMs"] == d["activeMs"] > 0 for d in days)


@_real_only
def test_real_wall_ms_is_filter_aware(real):
    """Narrowing to one role removes overlap, so wall can only move toward
    active -- never above it, and never further from it."""
    conn, _ = real
    for f in (Filters(role="root"), Filters(role="subagent"), Filters(sources=["codex"])):
        days = metrics.daily(conn, f)["days"]
        assert days
        assert all(0 <= d["wallMs"] <= d["activeMs"] for d in days)
        # A filtered day's wall time cannot exceed the unfiltered day's.
        unfiltered = {d["date"]: d["wallMs"] for d in metrics.daily(conn, Filters())["days"]}
        assert all(d["wallMs"] <= unfiltered[d["date"]] for d in days)


@_real_only
def test_day_and_hour_series_conserve_the_filtered_total(real):
    """Splitting spans must move milliseconds between buckets, never create or
    destroy them. Checked under a filter as well as unfiltered."""
    conn, _ = real
    for f in (Filters(), Filters(role="subagent"), Filters(sources=["codex"])):
        total = metrics.summary(conn, f)["activeMs"]
        assert sum(d["activeMs"] for d in metrics.daily(conn, f)["days"]) == total
        assert sum(c["activeMs"] for c in metrics.heatmap(conn, f)["cells"]) == total
        assert metrics.concurrency(conn, f)["activeMs"] == total


@_real_only
def test_real_corpus_groups_partition_active_time(real):
    """The invariant on 45 real project rows, whatever the detector has done.

    Before `cci group auto` has run there are no groups at all and the whole
    corpus is `ungrouped` -- which is the state this must also survive.
    """
    conn, cfg = real
    everything = metrics.summary(conn, Filters())["activeMs"]
    g = metrics.groups(conn, Filters())

    assert sum(r["activeMs"] for r in g["groups"]) + g["ungrouped"]["activeMs"] == everything
    for row in g["groups"]:
        assert row["projects"] >= 1 and row["pinnedProjects"] <= row["projects"]
        assert row["firstTs"] <= row["lastTs"] and row["activeMs"] >= 0

    ungrouped_rows = [p for p in metrics.projects(conn, Filters())["projects"]
                      if p["groupId"] is None]
    assert g["ungrouped"]["projects"] == len(ungrouped_rows)
    assert g["ungrouped"]["activeMs"] == sum(p["activeMs"] for p in ungrouped_rows)

    # meta is the roster: every ranked group is offered as a filter choice.
    offered = {row["groupId"] for row in metrics.meta(conn, cfg)["groups"]}
    assert {row["groupId"] for row in g["groups"]} <= offered


@_real_only
def test_real_corpus_group_filter_is_the_union_of_its_members(real, tmp_path):
    """A group laid over the three biggest real projects.

    Built on a BACKUP of the corpus, so the session's database is never
    written to -- `CC_INSIGHTS_TEST_CONFIG_DIR` may point it at a real one.
    """
    conn, _ = real
    grouped = sqlite3.connect(tmp_path / "grouped.db")
    conn.backup(grouped)
    grouped.row_factory = sqlite3.Row

    members = [p["projectId"] for p in metrics.projects(conn, Filters())["projects"][:3]]
    assert len(members) == 3, "the real corpus has fewer than three projects with time"
    now = db.now_ms()
    grouped.execute(
        "INSERT INTO project_group (group_id, name, origin, match_key, remote_url,"
        " forge, owner, repo, web_url, created_at, updated_at)"
        " VALUES ('g-test', 'test-group', 'git_remote', 'k', NULL, 'github', 'acme',"
        " 'test', 'https://github.com/acme/test', ?, ?)", (now, now))
    grouped.executemany("UPDATE project SET group_id = 'g-test' WHERE project_id = ?",
                        [(pid,) for pid in members])
    grouped.commit()

    by_group = metrics.summary(grouped, Filters(groups=["g-test"]))
    assert by_group == metrics.summary(grouped, Filters(projects=members))
    assert 0 < by_group["activeMs"] < metrics.summary(grouped, Filters())["activeMs"]

    g = metrics.groups(grouped, Filters())
    row = next(r for r in g["groups"] if r["groupId"] == "g-test")
    assert row["projects"] == 3 and row["pinnedProjects"] == 0
    assert row["activeMs"] == by_group["activeMs"]
    assert (sum(r["activeMs"] for r in g["groups"]) + g["ungrouped"]["activeMs"]
            == metrics.summary(grouped, Filters())["activeMs"])

    # The union arithmetic: the group plus one project outside it, and these
    # sets are disjoint, so the totals add.
    outside = next(p["projectId"] for p in metrics.projects(grouped, Filters())["projects"]
                   if p["projectId"] not in members)
    union = metrics.summary(grouped, Filters(groups=["g-test"], projects=[outside]))
    assert union["activeMs"] == by_group["activeMs"] + metrics.summary(
        grouped, Filters(projects=[outside]))["activeMs"]

    # ... and the split series conserve the filtered total, as everywhere else.
    f = Filters(groups=["g-test"])
    assert sum(d["activeMs"] for d in metrics.daily(grouped, f)["days"]) == by_group["activeMs"]
    assert sum(c["activeMs"] for c in metrics.heatmap(grouped, f)["cells"]) == by_group[
        "activeMs"]
    grouped.close()
