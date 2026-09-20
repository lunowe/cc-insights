"""Read-only summary queries behind `cci stats`.

Returns plain data structures; all rendering lives in cli.py. Timezone
conversion to local time happens here, once, so the CLI never touches UTC math.

Nothing in this module derives anything -- it reads what ingest and derive
already wrote. If a number here disagrees with docs/probes/canonical_metrics.py,
the pipeline is wrong, not this file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass(slots=True)
class Summary:
    hostname: str = ""
    first_ts: int | None = None
    last_ts: int | None = None
    sessions: int = 0
    threads: int = 0
    events: int = 0
    spans: int = 0
    by_source: list[tuple[str, float]] = field(default_factory=list)
    total_h: float = 0.0
    human_h: float = 0.0
    autonomous_h: float = 0.0
    unattended_root_h: float = 0.0
    projects: list[tuple[str, float]] = field(default_factory=list)
    agents: list[tuple[str, int, float]] = field(default_factory=list)
    models: list[tuple[str, int]] = field(default_factory=list)
    tokens: tuple[int, int, int] = (0, 0, 0)
    #: List-price equivalent of every priced event, in nano-currency-units,
    #: with the tokens no rate covered. Never a bill -- see cost.py.
    cost_nano: int = 0
    cost_currency: str = "USD"
    unpriced_tokens: int = 0


_H = 3_600_000.0  # ms per hour


def _scalar(conn: sqlite3.Connection, sql: str, *args) -> int:
    row = conn.execute(sql, args).fetchone()
    return (row[0] or 0) if row else 0


def summarize(conn: sqlite3.Connection) -> Summary:
    s = Summary()
    row = conn.execute("SELECT hostname FROM host ORDER BY last_seen DESC LIMIT 1").fetchone()
    s.hostname = row[0] if row else "unknown"

    s.sessions = _scalar(conn, "SELECT count(*) FROM session")
    s.threads = _scalar(conn, "SELECT count(*) FROM thread")
    s.events = _scalar(conn, "SELECT count(*) FROM event")
    s.spans = _scalar(conn, "SELECT count(*) FROM span")
    if not s.events:
        return s

    s.first_ts = _scalar(conn, "SELECT min(ts) FROM event") or None
    s.last_ts = _scalar(conn, "SELECT max(ts) FROM event") or None

    s.by_source = [
        (r[0], r[1] / _H)
        for r in conn.execute(
            """SELECT s.source, coalesce(sum(sp.ended_at - sp.started_at), 0)
               FROM span sp JOIN session s ON s.id = sp.session_id
               GROUP BY 1 ORDER BY 2 DESC"""
        )
    ]
    s.total_h = sum(h for _, h in s.by_source)

    # Three buckets that partition active time. `attended` is only ever asserted
    # for root threads: on a subagent thread it is a structural 0 meaning "a
    # model spawned this", not a measurement that nobody was watching.
    def bucket(where: str) -> float:
        return _scalar(
            conn,
            f"""SELECT coalesce(sum(sp.ended_at - sp.started_at), 0)
                FROM span sp JOIN thread t ON t.id = sp.thread_id WHERE {where}""",
        ) / _H

    s.human_h = bucket("t.is_subagent = 0 AND sp.attended = 1")
    s.autonomous_h = bucket("t.is_subagent = 1")
    s.unattended_root_h = bucket("t.is_subagent = 0 AND (sp.attended = 0 OR sp.attended IS NULL)")

    s.projects = [
        (r[0], r[1] / _H)
        for r in conn.execute(
            """SELECT p.name, sum(sp.ended_at - sp.started_at)
               FROM span sp
               JOIN session s ON s.id = sp.session_id
               JOIN project p ON p.project_id = s.project_id
               GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""
        )
    ]

    # Claude Code and opencode both record a real agent TYPE ("Explore",
    # "general"); Codex records a random per-thread nickname, which would
    # otherwise flood this list with one-offs.
    s.agents = [
        (r[0], r[1], r[2] / _H)
        for r in conn.execute(
            """SELECT t.agent_name, count(DISTINCT t.id), sum(sp.ended_at - sp.started_at)
               FROM thread t
               JOIN span sp ON sp.thread_id = t.id
               JOIN session s ON s.id = t.session_id
               WHERE t.agent_name IS NOT NULL
                 AND s.source IN ('claude_code', 'opencode')
               GROUP BY 1 ORDER BY 3 DESC LIMIT 8"""
        )
    ]

    s.models = [
        (r[0], r[1])
        for r in conn.execute(
            """SELECT model, count(*) FROM event
               WHERE model IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8"""
        )
    ]

    row = conn.execute(
        """SELECT coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0),
                  coalesce(sum(cache_read_tokens), 0) FROM event"""
    ).fetchone()
    s.tokens = (row[0], row[1], row[2])

    s.cost_nano = _scalar(
        conn,
        """SELECT coalesce(sum(input_nano + output_nano + cache_read_nano
                            + cache_write_nano), 0) FROM event_cost""",
    )
    # Tokens on events that were never priced. The anti-join is the whole
    # point: a total is only quotable next to what it could not see.
    s.unpriced_tokens = _scalar(
        conn,
        """SELECT coalesce(sum(coalesce(e.input_tokens, 0) + coalesce(e.output_tokens, 0)
                            + coalesce(e.cache_read_tokens, 0)
                            + coalesce(e.cache_write_tokens, 0)), 0)
           FROM event e
           WHERE NOT EXISTS (SELECT 1 FROM event_cost c WHERE c.event_id = e.id)""",
    )
    currencies = [r[0] for r in conn.execute(
        """SELECT DISTINCT p.currency FROM model_price p
           WHERE EXISTS (SELECT 1 FROM event_cost c WHERE c.model = p.model)"""
    )]
    s.cost_currency = currencies[0] if len(currencies) == 1 else "mixed"
    return s
