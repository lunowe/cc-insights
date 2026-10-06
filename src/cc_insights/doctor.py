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

from cc_insights import account, assets, config as config_mod, db, remote, scheduler

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: How stale the last ingest may be before it is worth saying something. The
#: interval job runs every 15 minutes, so an hour is four missed runs -- past
#: coincidence, short of panic. A day is long enough that logs may already
#: have aged out from under it.
STALE_WARN_S = 3600
STALE_FAIL_S = 86_400

#: How stale the last successful push may be before it is worth mentioning.
#: A day, where a missed *ingest* is an hour, and the gap is the point: a push
#: that has not run loses nothing. Local SQLite stays the source of truth, so
#: the only cost is that the other machine's view is behind. That is a WARN
#: and never a FAIL -- reserving FAIL for the failures that destroy history
#: is what keeps a FAIL worth reading.
PUSH_STALE_WARN_S = 86_400

#: How long `doctor` will wait on the account server. Short: this command is
#: meant to be cheap to ask, and an unreachable server is itself the answer.
ACCOUNT_TIMEOUT_S = 5.0


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
            "no scheduler here (no launchd, Task Scheduler or crontab), so "
            "doctor cannot see a job you scheduled yourself",
            "cci install  (prints the job line to schedule yourself)",
        ))
    elif (unreadable := scheduler.cron_error()) is not None:
        out.append(Check(
            "background job", FAIL,
            f"cannot read your crontab, so whether the job is there is unknown: {unreadable}",
            "crontab -l  (fix whatever it reports, then `cci install`)",
        ))
    else:
        job = scheduler.active()
        if job is not None:
            kind = "follows the logs live" if job.mode == scheduler.WATCH \
                else "every 15 minutes"
            out.append(Check("background job", OK, f"{job.label} — {kind}"))
        else:
            stopped = [j for j in scheduler.status() if j.installed and not j.loaded]
            if stopped and scheduler.uses_cron():
                out.append(Check(
                    "background job", FAIL,
                    f"{stopped[0].label} is in your crontab, but no cron daemon "
                    "is running to read it",
                    "start cron  (`sudo systemctl enable --now cron`, or `crond` "
                    "on Fedora, RHEL and Alpine)",
                ))
            elif stopped:
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


def _schema(cfg: config_mod.Config, conn: sqlite3.Connection | None,
            unreadable: str | None = None) -> list[Check]:
    if conn is None:
        if unreadable is not None:
            # "does not exist — run `cci init`" would be false and the advice
            # wrong: the file is right there, and `cci init` opens it and
            # fails the same way. Say what SQLite said, and name the one move
            # that gets the tool running again.
            return [Check(
                "database", FAIL,
                f"{cfg.db_path} exists but SQLite cannot read it: {unreadable}",
                "move that file aside and run `cci init`  (a corrupt database "
                "is not recoverable by this tool)",
            )]
        return [Check("database", FAIL, f"{cfg.db_path} does not exist", "cci init")]

    try:
        applied = db.applied_versions(conn)
        migrations = db.discover_migrations()
    except RuntimeError as exc:
        # `discover_migrations` raises when the installed package shipped no
        # .sql files. Doctor is the command you run to be *told* about a
        # packaging bug like that; dying on it left the user with a traceback
        # from the health check instead of the diagnosis.
        return [Check("schema", FAIL, str(exc), "reinstall cc-insights")]
    except sqlite3.Error as exc:
        return [Check("schema", FAIL, f"the schema could not be read: {exc}",
                      "cci init")]

    pending = [v for v, _ in migrations if v not in applied]
    if pending:
        return [Check("schema", FAIL, f"pending migrations: {pending}", "cci init")]
    if not applied:                                      # pragma: no cover
        # Unreachable while `discover_migrations` refuses to find none -- an
        # unmigrated database lands in the `pending` branch above. Kept
        # because `max()` of an empty set raises, and doctor must never be
        # the command that crashes.
        return [Check("schema", FAIL, "no migrations have been applied", "cci init")]
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


def _account(cfg: config_mod.Config) -> list[Check]:
    """Signed in, still accepted, and actually pushing. Three failures, three checks.

    They are separate because the fixes are: `cci login`, nothing (wait for
    the network), and `cci sync push`. Collapsing them into one "account"
    line would name at most one of those, and the other two would be the ones
    somebody had.

    Not being signed in is OK, not a warning. Local-first is the default and
    the tool is complete without an account; saying otherwise would nag every
    single-machine user forever.
    """
    credential = account.load(cfg.config_dir)
    token = account.token_for(credential)
    url = config_mod.server_url_for(
        cfg, None, credential.server_url if credential is not None else None
    )
    if not token:
        return [Check("account", OK, "not signed in — this machine only")]
    if not url:
        return [Check(
            "account", WARN,
            "a token is set but no server URL is configured",
            "cci login --server https://your-instance",
        )]

    out: list[Check] = []
    account_id = credential.account_id if credential is not None else None
    try:
        who = remote.Client(url, token, timeout_s=ACCOUNT_TIMEOUT_S).whoami()
    except remote.AuthRequired:
        out.append(Check("account", FAIL,
                         f"{url} rejected this credential — it is expired or revoked",
                         "cci login"))
        who = None
    except remote.Unreachable as exc:
        # A WARN, never a FAIL. The server being down costs nothing locally,
        # and a red line here would send somebody looking for a broken
        # install when the answer is "try again later".
        out.append(Check("account", WARN,
                         f"cannot reach {url} ({exc.code or 'network'}) — "
                         "capture is unaffected",
                         "check the network; nothing local is broken"))
        who = None
    except Exception as exc:                             # pragma: no cover
        # Doctor must never be the command that crashes on a broken install.
        out.append(Check("account", WARN, f"could not be checked: {exc}", None))
        who = None
    else:
        teams = who.get("teams") or []
        detail = f"signed in as {who.get('actor')} at {url}"
        if teams:
            detail += f" — {len(teams)} team(s)"
        out.append(Check("account", OK, detail))
        account_id = who.get("accountId") or account_id

    if account_id:
        out.append(_last_push(cfg, url, account_id))
    return out


def _last_push(cfg: config_mod.Config, url: str, account_id: str) -> Check:
    try:
        last = remote.last_push_at(cfg.config_dir, url, account_id)
    except Exception:                                    # pragma: no cover
        return Check("last push", WARN, "the transfer state could not be read",
                     "cci sync push")
    if last is None:
        return Check("last push", WARN,
                     "never — your other machines cannot see this one yet",
                     "cci sync push")
    age = max(0.0, time.time() - last / 1000)
    if age < PUSH_STALE_WARN_S:
        return Check("last push", OK, _ago(age))
    return Check("last push", WARN, _ago(age) + " — your other machines are behind",
                 "cci sync push  (or `cci install` to schedule it)")


def _auto_push(cfg: config_mod.Config) -> list[Check]:
    """Whether the installed job actually pushes, for somebody who is signed in.

    An upgrade that adds a step to the pipeline does not rewrite the job file
    already on disk, so a machine that signed in after installing keeps
    running the old line and never pushes. Nothing errors and nothing is
    lost -- the second machine is simply, permanently, out of date, which is
    the product ask quietly not working.
    """
    if not account.token_for(account.load(cfg.config_dir)):
        return []
    job = scheduler.active()
    if job is None:
        return []
    command = scheduler.installed_command(job.mode)
    if command is None:
        return []                       # cannot tell; never reported as a fault
    if job.mode == scheduler.WATCH:
        # `cci watch` pushes from inside its own loop (see `_auto_pusher` in
        # cli.py), so the job line has nothing to say about it either way.
        return [Check("auto-push", OK, "the watcher pushes as it goes")]
    if "sync auto" in command:
        return [Check("auto-push", OK, "the background job pushes after each run")]
    return [Check(
        "auto-push", WARN,
        "the background job predates auto-push, so this machine only pushes "
        "when you run `cci sync push` by hand",
        "cci install  (rewrites the job)",
    )]


def run(cfg: config_mod.Config) -> list[Check]:
    """Every check, in the order they matter. Never raises on a bad install.

    That promise is the whole contract of this module, and it was not true:
    `scripts/install.sh` ends with `cci doctor`, so the two installs doctor
    exists to diagnose -- a package shipped without its migrations, and a
    first `cci init` that was interrupted -- greeted the user with a
    traceback out of the health check instead of a report naming the fix.

    The order below is deliberate. The schema is settled first because
    everything after it queries tables that only exist if the answer was
    "up to date": on a database file with no tables, `_capture` used to die
    with `no such table: ingest_file`. When the schema is not usable the
    connection is withheld from the later checks, which then say what they
    can (the background job does not need the database) and stay quiet
    about the rest.
    """
    conn: sqlite3.Connection | None = None
    unreadable: str | None = None
    if cfg.db_path.exists():
        try:
            conn = db.connect(cfg.db_path)
        except sqlite3.Error as exc:
            # A corrupt file, or one that is not a database at all. Keep the
            # reason: without it `_schema` reported "does not exist", which
            # is false and sends the reader to the wrong fix.
            conn, unreadable = None, str(exc)

    checks: list[Check] = []
    try:
        schema = _schema(cfg, conn, unreadable)
        usable = conn if all(c.level == OK for c in schema) else None
        checks += schema + _capture(cfg, usable) + _contents(usable)
        checks += _dashboard() + _sync(cfg)
        # Account last: it is the only check that touches the network, so a
        # slow or unreachable server delays the answer to "is it capturing?"
        # by as little as possible -- and that question is the urgent one.
        checks += _account(cfg) + _auto_push(cfg)
    except Exception as exc:                             # pragma: no cover
        # The last resort, for the failure nobody predicted. A report that
        # stops early and says so is still usable from a script; a traceback
        # out of the command that is supposed to explain a broken install is
        # not, and it is also what the caller least expects.
        checks.append(Check(
            "doctor", FAIL, f"the checks stopped early: {exc!r}",
            "please report this, with the line above",
        ))
    finally:
        if conn is not None:
            conn.close()
    return checks


def worst(checks: list[Check]) -> str:
    for level in (FAIL, WARN):
        if any(c.level == level for c in checks):
            return level
    return OK
