"""Is this installation actually working? One command that answers it.

Every other command reports on the *data*. This one reports on the
*installation*, and it exists because the failure this tool is most exposed
to is silent. Agent log directories are pruned on a rolling basis: if the
background job is not running, nothing breaks, nothing errors, and no page
goes blank -- the numbers just quietly stop moving, and by the time anyone
notices, the logs that would have filled the gap are gone.

So the question "is it capturing?" has to be cheap to ask and impossible to
misread. The checks below are ordered by what they would cost you: capture
first, then the schema, then the parts you would notice yourself.

Freshness is measured against `ingest_file.last_ingest` -- when the tool last
*looked* -- and not against the newest event, because those two answer
different questions. No events for three days is a quiet week. No ingest for
three days is a broken install, and only the second one is an emergency.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cc_insights import assets, config as config_mod, db, scheduler

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: How stale the last ingest may be before it is worth saying something. The
#: interval job runs every 15 minutes, so an hour is four missed runs -- past
#: coincidence, short of panic. A day is long enough that logs may already
#: have aged out from under it.
STALE_WARN_S = 3600
STALE_FAIL_S = 86_400


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    detail: str
    fix: str | None = None


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86_400:.1f}d ago"


def source_files(cfg: config_mod.Config) -> int:
    """How many agent log files the configured globs match right now, or -1.

    This is what separates "the tool has never run" from "the tool ran and
    there was nothing to read", which `ingest_file` cannot distinguish: it
    only ever gets a row for a file that was found. Without the difference,
    a brand-new machine whose agents have not written anything yet -- or
    whose logs are somewhere the globs do not look -- is told its install
    has FAILED on the first run after `curl | sh`, which is both wrong and
    the worst possible first impression.

    -1 means the question could not be answered (a broken glob, an adapter
    that raised). Doctor must never be the command that crashes.
    """
    try:
        from cc_insights import ingest
        return sum(1 for adapter in ingest.build_adapters(cfg) for _ in adapter.discover())
    except Exception:                                    # pragma: no cover
        return -1


def _capture(cfg: config_mod.Config, conn: sqlite3.Connection | None) -> list[Check]:
    """The job, and whether it has run recently. The two halves of one answer.

    Both are needed: a job can be loaded and still failing every time (a
    stale path to `cci` after a venv was rebuilt, say), and a database can be
    fresh because someone ran `cci ingest` by hand ten minutes ago while no
    job exists at all. Neither check alone distinguishes those from working.
    """
    out: list[Check] = []

    if not scheduler.supported():
        out.append(Check(
            "background job", WARN,
            "no scheduler integration on this platform",
            "cci install  (prints a cron line you can use instead)",
        ))
    else:
        job = scheduler.active()
        if job is not None:
            kind = "follows the logs live" if job.mode == scheduler.WATCH \
                else "every 15 minutes"
            out.append(Check("background job", OK, f"{job.label} — {kind}"))
        else:
            stopped = [j for j in scheduler.status() if j.installed and not j.loaded]
            if stopped:
                out.append(Check(
                    "background job", FAIL,
                    f"{stopped[0].label} is installed but not loaded",
                    "cci install  (reinstalls and reloads it)",
                ))
            else:
                out.append(Check(
                    "background job", FAIL,
                    "not installed — history is only captured when you run "
                    "`cci ingest` by hand, and agent logs are pruned on a "
                    "rolling basis",
                    "cci install",
                ))

    if conn is None:
        return out

    row = conn.execute("SELECT max(last_ingest) FROM ingest_file").fetchone()
    last = row[0] if row else None
    if last is None:
        found = source_files(cfg)
        if found == 0:
            # Nothing to read is not a broken install. Say where it looked,
            # because the actionable case -- logs in a non-default location --
            # is indistinguishable from the innocent one without that.
            out.append(Check(
                "agent logs", WARN,
                "none found in the configured locations — nothing to capture yet",
                "cci config  (check source_globs if your agents keep logs elsewhere)",
            ))
        else:
            waiting = "" if found < 0 else f" — {found:,} log files are waiting"
            out.append(Check("last ingest", FAIL, f"never{waiting}", "cci ingest"))
    else:
        age = max(0.0, time.time() - last / 1000)
        level = OK if age < STALE_WARN_S else (WARN if age < STALE_FAIL_S else FAIL)
        fix = None if level == OK else "cci ingest  (and check `cci doctor` again)"
        out.append(Check("last ingest", level, _ago(age), fix))

    return out


def _schema(cfg: config_mod.Config, conn: sqlite3.Connection | None) -> list[Check]:
    if conn is None:
        return [Check("database", FAIL, f"{cfg.db_path} does not exist", "cci init")]

    applied = db.applied_versions(conn)
    pending = [v for v, _ in db.discover_migrations() if v not in applied]
    if pending:
        return [Check("schema", FAIL, f"pending migrations: {pending}", "cci init")]
    return [Check("schema", OK, f"up to date (through {max(applied)})")]


def _contents(conn: sqlite3.Connection | None) -> list[Check]:
    if conn is None:
        return []
    n_sessions, n_events = conn.execute(
        "SELECT (SELECT count(*) FROM session), (SELECT count(*) FROM event)"
    ).fetchone()
    if n_events == 0:
        return [Check(
            "data", WARN, "no events yet",
            "cci ingest  (then `cci derive`) — or check `cci config` "
            "if your agent logs are not in the usual place",
        )]

    n_spans = conn.execute("SELECT count(*) FROM span").fetchone()[0]
    detail = f"{n_sessions:,} sessions, {n_events:,} events, {n_spans:,} spans"
    if n_spans == 0:
        return [Check("data", WARN, detail + " — nothing derived", "cci derive")]
    return [Check("data", OK, detail)]


def _dashboard() -> list[Check]:
    """A missing dashboard means something different in each situation.

    In a wheel it is a broken release and the user can do nothing about it.
    In a checkout it is the normal state before the frontend is built, and
    the fix is one command they can actually run. Saying "not built" in the
    first case sends someone looking for a `frontend/` directory that is not
    on their disk.
    """
    where = assets.frontend_dir()
    if where.is_dir():
        return [Check("dashboard", OK, str(where))]
    if assets.is_packaged():                             # pragma: no cover
        return [Check("dashboard", FAIL,
                      "missing from the installed package — this is a packaging bug",
                      "reinstall cc-insights")]
    return [Check("dashboard", WARN, "not built — the JSON API still works",
                  "cd frontend && pnpm install && pnpm build")]


def _sync(cfg: config_mod.Config) -> list[Check]:
    url = config_mod.sync_url_for(cfg)
    if not url:
        return [Check("sync", OK, "not configured — this machine only")]
    try:
        import psycopg                                   # noqa: F401
    except ModuleNotFoundError:
        return [Check("sync", FAIL, "a sync URL is set but the driver is missing",
                      "pip install 'cc-insights[postgres]'")]
    # The URL may carry a password; `config.py` says so and so does this.
    return [Check("sync", OK, "configured")]


def run(cfg: config_mod.Config) -> list[Check]:
    """Every check, in the order they matter. Never raises on a bad install."""
    conn: sqlite3.Connection | None = None
    if cfg.db_path.exists():
        try:
            conn = db.connect(cfg.db_path)
        except sqlite3.Error:                            # pragma: no cover
            conn = None
    try:
        checks = _schema(cfg, conn) + _capture(cfg, conn) + _contents(conn)
        checks += _dashboard() + _sync(cfg)
    finally:
        if conn is not None:
            conn.close()
    return checks


def worst(checks: list[Check]) -> str:
    for level in (FAIL, WARN):
        if any(c.level == level for c in checks):
            return level
    return OK
