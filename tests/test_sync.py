"""Multi-machine sync.

Most of this runs SQLite -> SQLite. That is not a shortcut around the real
thing: the transfer is one set of statements generated from `sync.TABLES`, and
what it has to get right -- ownership, FK order, self-referencing threads, the
pin rule, idempotence -- is engine-independent. Running it without a server
means it runs on every machine, every time, instead of only where someone
remembered to start PostgreSQL.

What is genuinely engine-specific -- INTEGER widening, `information_schema`,
psycopg's transaction handling, `%s` placeholders -- is covered at the bottom
against a real PostgreSQL, skipped when there is none. Those are the tests that
would pass against a mock and still ship a broken release.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from cc_insights import db, sync

MAC = "host-mac"
PC = "host-pc"


# ------------------------------------------------------------------ fixtures --


def fresh(tmp_path, name: str) -> sqlite3.Connection:
    conn = db.connect(tmp_path / f"{name}.db")
    db.migrate(conn)
    return conn


def add_host(conn, host_id, hostname="box", os_name="test", first=1, last=2):
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, ?, ?, ?, ?)",
        (host_id, hostname, os_name, first, last),
    )


def add_session(conn, host_id, sid, project_id=None, *, started=100, ended=200, active=50):
    conn.execute(
        """INSERT INTO session (id, native_id, source, host_id, project_id,
                                started_at, ended_at, active_ms)
           VALUES (?, ?, 'claude_code', ?, ?, ?, ?, ?)""",
        (sid, sid, host_id, project_id, started, ended, active),
    )


def add_thread(conn, tid, sid, parent=None, *, started=100, ended=200):
    conn.execute(
        """INSERT INTO thread (id, native_id, session_id, parent_thread_id,
                               is_subagent, started_at, ended_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (tid, tid, sid, parent, 1 if parent else 0, started, ended),
    )


def add_project(conn, pid, root_path, *, group_id=None, pinned=0):
    conn.execute(
        "INSERT INTO project (project_id, root_path, name, group_id, group_pinned)"
        " VALUES (?, ?, ?, ?, ?)",
        (pid, root_path, root_path.rsplit("/", 1)[-1], group_id, pinned),
    )


def add_group(conn, gid, name, origin="manual", match_key=None):
    conn.execute(
        """INSERT INTO project_group (group_id, name, origin, match_key,
                                      created_at, updated_at)
           VALUES (?, ?, ?, ?, 0, 0)""",
        (gid, name, origin, match_key),
    )


def counts(conn) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("host", "project_group", "project", "project_probe",
                  "session", "thread", "event", "span")
    }


@pytest.fixture
def mac(tmp_path):
    conn = fresh(tmp_path, "mac")
    add_host(conn, MAC, "macbook", "Darwin 27.0.0")
    add_project(conn, "p1", "/Users/me/Coding/repo")
    add_session(conn, MAC, "s-mac", "p1")
    add_thread(conn, "t-mac", "s-mac")
    yield conn
    conn.close()


@pytest.fixture
def shared(tmp_path):
    """The meeting point, standing in for PostgreSQL."""
    conn = fresh(tmp_path, "shared")
    yield conn
    conn.close()


def push(local, remote, host_id):
    return sync.push(local, remote, host_id, dialect=db.SQLITE)


def pull(local, remote):
    return sync.pull(local, remote, dialect=db.SQLITE)


# ----------------------------------------------------------------- the basics --


def test_push_then_pull_round_trips(mac, shared, tmp_path):
    push(mac, shared, MAC)
    assert counts(shared)["session"] == 1

    other = fresh(tmp_path, "other")
    pull(other, shared)
    assert counts(other) == counts(mac)
    other.close()


def test_a_second_push_changes_nothing(mac, shared):
    push(mac, shared, MAC)
    before = counts(shared)
    push(mac, shared, MAC)
    assert counts(shared) == before, "ids are content hashes; a re-push must collapse"


def test_a_row_that_grew_locally_is_updated_not_duplicated(mac, shared):
    """A session keeps getting longer as its log does. The remote must follow."""
    push(mac, shared, MAC)
    mac.execute("UPDATE session SET ended_at = 999, active_ms = 800 WHERE id = 's-mac'")

    push(mac, shared, MAC)

    row = shared.execute("SELECT ended_at, active_ms FROM session WHERE id='s-mac'").fetchone()
    assert (row[0], row[1]) == (999, 800)
    assert counts(shared)["session"] == 1


def test_two_machines_land_side_by_side(mac, shared, tmp_path):
    pc = fresh(tmp_path, "pc")
    add_host(pc, PC, "winbox", "Windows 11")
    add_project(pc, "p2", r"C:\Users\you\Coding\repo")
    add_session(pc, PC, "s-pc", "p2")

    push(mac, shared, MAC)
    push(pc, shared, PC)

    assert counts(shared)["host"] == 2
    assert counts(shared)["session"] == 2
    paths = {r[0] for r in shared.execute("SELECT root_path FROM project")}
    assert paths == {"/Users/me/Coding/repo", r"C:\Users\you\Coding\repo"}
    pc.close()


def test_pull_brings_back_the_other_machine(mac, shared, tmp_path):
    pc = fresh(tmp_path, "pc")
    add_host(pc, PC, "winbox", "Windows 11")
    add_session(pc, PC, "s-pc")
    push(pc, shared, PC)
    pc.close()

    push(mac, shared, MAC)
    pull(mac, shared)

    assert counts(mac)["session"] == 2
    assert {r[0] for r in mac.execute("SELECT host_id FROM host")} == {MAC, PC}


# -------------------------------------------------------------------- ownership --


def test_a_host_pushes_only_its_own_rows(mac, shared, tmp_path):
    """After a pull the local database holds other machines' rows too.

    Pushing them back would be harmless but pointless, and it turns every
    machine's push into O(everyone). Ownership keeps it O(mine).
    """
    pc = fresh(tmp_path, "pc")
    add_host(pc, PC)
    add_session(pc, PC, "s-pc")
    push(pc, shared, PC)
    pc.close()
    pull(mac, shared)
    assert counts(mac)["session"] == 2

    stats = push(mac, shared, MAC)
    assert stats.rows["session"] == 1, "only the mac's own session should be sent"


def test_probe_rows_travel_with_their_machine(mac, shared):
    mac.execute(
        "INSERT INTO project_probe (project_id, host_id, path_exists, detected_at)"
        " VALUES ('p1', ?, 1, 7)", (MAC,)
    )
    push(mac, shared, MAC)
    row = shared.execute("SELECT host_id, path_exists FROM project_probe").fetchone()
    assert (row[0], row[1]) == (MAC, 1)


def test_ingest_file_never_leaves_the_machine(mac, shared):
    """Local bookkeeping whose only content is a full local path."""
    mac.execute(
        """INSERT INTO ingest_file (host_id, path, source, size_bytes, mtime_ms,
                                    bytes_read, lines_read, last_ingest)
           VALUES (?, '/Users/me/.claude/projects/secret-client/x.jsonl',
                   'claude_code', 1, 1, 1, 1, 1)""",
        (MAC,),
    )
    push(mac, shared, MAC)
    assert "ingest_file" not in [t.name for t in sync.TABLES]
    assert shared.execute("SELECT count(*) FROM ingest_file").fetchone()[0] == 0


# ------------------------------------------------------------ ordering and FKs --


def test_a_deeply_nested_subagent_does_not_break_the_foreign_key(mac, shared):
    """A workflow's agents are children of an agent. Depth is not bounded at 1."""
    add_thread(mac, "t-kid", "s-mac", parent="t-mac")
    add_thread(mac, "t-grandkid", "s-mac", parent="t-kid")
    add_thread(mac, "t-greatgrandkid", "s-mac", parent="t-grandkid")

    push(mac, shared, MAC)

    assert counts(shared)["thread"] == 4


def test_a_child_stored_before_its_parent_still_pushes(mac, shared):
    """The row order the source hands back is not the order we may insert in.

    A subagent thread is created when its `Agent` tool_use is seen and linked
    to its parent afterwards, so the child genuinely can sit earlier in the
    table. `SELECT` with no `ORDER BY` then returns it first, and inserting it
    first violates `thread.parent_thread_id`. Reproduced here by linking after
    the fact, which is exactly how ingest does it.
    """
    # Inserted child-first AND named child-first, so the row comes back ahead
    # of its parent whether SQLite scans the table or the primary-key index.
    add_thread(mac, "t-aaa-child", "s-mac", parent=None)
    add_thread(mac, "t-zzz-parent", "s-mac", parent=None)
    mac.execute("UPDATE thread SET parent_thread_id = 't-zzz-parent' WHERE id = 't-aaa-child'")

    table = next(t for t in sync.TABLES if t.name == "thread")
    natural = [
        r[table.columns.index("id")]
        for r in mac.execute(sync.select_sql(table, owned=True), (MAC,))
    ]
    assert natural.index("t-aaa-child") < natural.index("t-zzz-parent"), (
        "precondition: the child must come back first, or this proves nothing"
    )

    push(mac, shared, MAC)

    assert counts(shared)["thread"] == 3


def test_children_are_ordered_after_parents_whatever_the_row_order():
    table = next(t for t in sync.TABLES if t.name == "thread")
    id_at = table.columns.index("id")
    parent_at = table.columns.index("parent_thread_id")

    def row(tid, parent):
        cells = [None] * len(table.columns)
        cells[id_at], cells[parent_at] = tid, parent
        return tuple(cells)

    # Deliberately worst case: every child precedes its parent.
    rows = [row("c", "b"), row("b", "a"), row("a", None)]
    ordered = [r[id_at] for r in sync.order_by_parent(table, rows)]
    assert ordered.index("a") < ordered.index("b") < ordered.index("c")


def test_a_parent_cycle_terminates_instead_of_hanging():
    """Corrupt data must not become an infinite recursion inside a transaction."""
    table = next(t for t in sync.TABLES if t.name == "thread")
    id_at = table.columns.index("id")
    parent_at = table.columns.index("parent_thread_id")

    def row(tid, parent):
        cells = [None] * len(table.columns)
        cells[id_at], cells[parent_at] = tid, parent
        return tuple(cells)

    out = sync.order_by_parent(table, [row("x", "y"), row("y", "x")])
    assert len(out) == 2


def test_tables_are_declared_in_foreign_key_order():
    """project.group_id points at project_group, so the group has to go first."""
    order = [t.name for t in sync.TABLES]
    assert order.index("project_group") < order.index("project")
    assert order.index("project") < order.index("project_probe")
    assert order.index("host") < order.index("session")
    assert order.index("session") < order.index("thread")
    assert order.index("thread") < order.index("event")
    assert order.index("thread") < order.index("span")


# ------------------------------------------------------------------- conflicts --


def test_a_pin_survives_a_push_from_a_machine_that_never_heard_of_it(mac, shared, tmp_path):
    """A pin is a human saying where a project belongs.

    `cci group auto` is careful never to overwrite one locally. A push from a
    second machine is the same event arriving by another route, and it has to
    obey the same rule -- otherwise every sync silently undoes a decision
    somebody made by hand.
    """
    add_group(mac, "g-manual", "Client Work")
    push(mac, shared, MAC)

    # The shared database has the pin; this machine does not know about it.
    shared.execute(
        "UPDATE project SET group_id = 'g-manual', group_pinned = 1 WHERE project_id = 'p1'"
    )
    mac.execute("UPDATE project SET group_id = NULL, group_pinned = 0 WHERE project_id = 'p1'")

    push(mac, shared, MAC)

    row = shared.execute(
        "SELECT group_id, group_pinned FROM project WHERE project_id='p1'"
    ).fetchone()
    assert (row[0], row[1]) == ("g-manual", 1)


def test_an_unpinned_group_still_follows_detection(mac, shared):
    add_group(mac, "g-auto", "repo", origin="git_remote", match_key="https://x/repo")
    push(mac, shared, MAC)
    mac.execute("UPDATE project SET group_id = 'g-auto' WHERE project_id = 'p1'")

    push(mac, shared, MAC)

    assert shared.execute(
        "SELECT group_id FROM project WHERE project_id='p1'"
    ).fetchone()[0] == "g-auto"


def test_first_seen_only_ever_moves_backwards(mac, shared):
    push(mac, shared, MAC)
    shared.execute("UPDATE host SET first_seen = 10, last_seen = 20 WHERE host_id = ?", (MAC,))
    mac.execute("UPDATE host SET first_seen = 50, last_seen = 60 WHERE host_id = ?", (MAC,))

    push(mac, shared, MAC)

    row = shared.execute("SELECT first_seen, last_seen FROM host WHERE host_id=?", (MAC,)).fetchone()
    assert (row[0], row[1]) == (10, 60), "earliest first_seen, latest last_seen"


def test_a_failed_push_leaves_the_shared_database_untouched(mac, shared, monkeypatch):
    """Half a host is worse than none: sessions without their events make every
    number on the dashboard wrong in a way that looks plausible."""
    add_session(mac, MAC, "s-two", "p1")
    real = sync.upsert_sql

    def explode(table, dialect):
        if table.name == "session":
            raise RuntimeError("network died mid-push")
        return real(table, dialect)

    monkeypatch.setattr(sync, "upsert_sql", explode)
    with pytest.raises(RuntimeError):
        push(mac, shared, MAC)

    # host, project_group, project and project_probe all went across before
    # `session` blew up. Every one of them has to be gone again.
    assert set(counts(shared).values()) == {0}, counts(shared)


# ------------------------------------------------------------------ statements --


def test_the_generated_upsert_targets_the_primary_key():
    table = next(t for t in sync.TABLES if t.name == "project_probe")
    sql = sync.upsert_sql(table, db.SQLITE)
    assert "ON CONFLICT (project_id, host_id) DO UPDATE SET" in sql
    assert "git_remote = excluded.git_remote" in sql


def test_placeholders_are_rewritten_for_postgres():
    table = next(t for t in sync.TABLES if t.name == "span")
    assert "?" in sync.upsert_sql(table, db.SQLITE)
    pg = sync.upsert_sql(table, db.POSTGRES)
    assert "?" not in pg and "%s" in pg


def test_every_declared_column_exists(mac):
    """A typo here is a runtime error on somebody else's machine."""
    for table in sync.TABLES:
        actual = {r[1] for r in mac.execute(f"PRAGMA table_info({table.name})")}
        assert set(table.columns) <= actual, f"{table.name}: {set(table.columns) - actual}"
        assert set(table.key) <= set(table.columns)


def test_the_transfer_covers_every_table_that_carries_insight():
    """A new table must be added here consciously, not forgotten silently."""
    synced = {t.name for t in sync.TABLES}
    known = {"host", "project", "project_group", "project_probe",
             "session", "thread", "event", "span"}
    assert synced == known, (
        "sync.TABLES drifted from the schema. Add the table, or add it to the "
        "deliberately-excluded list with a reason."
    )


# ------------------------------------------------------------------------ cli --


def test_sync_without_a_configured_url_says_how_to_configure_one(tmp_path, capsys, monkeypatch):
    """The failure a first-time user hits. It has to name all three ways in."""
    from cc_insights import cli

    monkeypatch.delenv("CC_INSIGHTS_SYNC_URL", raising=False)
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit):
        cli.main(["--config-dir", str(tmp_path), "sync", "push"])

    err = capsys.readouterr().err
    assert "--url" in err and "CC_INSIGHTS_SYNC_URL" in err and "sync_url" in err


def test_an_unreachable_database_fails_with_its_reason(tmp_path, capsys, monkeypatch):
    """Not a traceback: this runs from a 15-minute background job."""
    from cc_insights import cli

    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql://nobody@127.0.0.1:1/nope")
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    capsys.readouterr()

    with pytest.raises(SystemExit):
        cli.main(["--config-dir", str(tmp_path), "sync", "push"])

    assert "cannot reach the shared database" in capsys.readouterr().err


def test_the_flag_beats_the_environment_beats_the_config(tmp_path, monkeypatch):
    from cc_insights import config as config_mod

    cfg = config_mod.load(tmp_path)
    cfg.sync_url = "postgresql://from-config/db"
    cfg.save()

    monkeypatch.delenv("CC_INSIGHTS_SYNC_URL", raising=False)
    assert config_mod.sync_url_for(cfg) == "postgresql://from-config/db"

    monkeypatch.setenv("CC_INSIGHTS_SYNC_URL", "postgresql://from-env/db")
    assert config_mod.sync_url_for(cfg) == "postgresql://from-env/db"
    assert config_mod.sync_url_for(cfg, "postgresql://from-flag/db") == "postgresql://from-flag/db"


def test_a_sync_url_round_trips_through_the_config_file(tmp_path):
    from cc_insights import config as config_mod

    cfg = config_mod.load(tmp_path)
    cfg.sync_url = "postgresql://me:p%40ss@db.example.com:5432/cci"
    cfg.save()
    assert config_mod.load(tmp_path, create=False).sync_url == cfg.sync_url


# ------------------------------------------------ the real thing, if available --

PG_URL = os.environ.get("CC_INSIGHTS_TEST_PG_URL")


def _psycopg():
    try:
        import psycopg
    except ModuleNotFoundError:
        return None
    return psycopg


needs_pg = pytest.mark.skipif(
    not PG_URL or _psycopg() is None,
    reason="set CC_INSIGHTS_TEST_PG_URL and install psycopg to run the PostgreSQL tests",
)


@pytest.fixture
def pg():
    psycopg = _psycopg()
    conn = psycopg.connect(PG_URL, autocommit=True)
    conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db.migrate(conn, dialect=db.POSTGRES)
    yield conn
    conn.close()


@needs_pg
def test_migrations_apply_to_postgres_and_are_idempotent(pg):
    assert db.migrate(pg, dialect=db.POSTGRES) == [], "a second run must be a no-op"
    tables = {
        r[0]
        for r in pg.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
    }
    assert {"host", "project", "project_probe", "session", "thread", "event", "span"} <= tables


@needs_pg
def test_no_column_is_int4(pg):
    """Epoch-ms is ~1.79e12 and PostgreSQL INTEGER tops out at 2.1e9.

    Without the widening this does not fail loudly at migration time -- it
    fails years later, on the first push, with every timestamp out of range.
    """
    narrow = pg.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND data_type = 'integer'"
    ).fetchall()
    assert narrow == []


@needs_pg
def test_an_epoch_ms_timestamp_survives_postgres(pg, mac):
    far_future = 1_789_000_000_000
    mac.execute("UPDATE session SET started_at = ?, ended_at = ?", (far_future, far_future + 1))
    sync.push(mac, pg, MAC)
    got = pg.execute("SELECT started_at FROM session WHERE id = 's-mac'").fetchone()[0]
    assert got == far_future


@needs_pg
def test_push_and_pull_against_a_real_postgres(pg, mac, tmp_path):
    add_thread(mac, "t-kid", "s-mac", parent="t-mac")
    mac.execute(
        "INSERT INTO project_probe (project_id, host_id, path_exists, detected_at)"
        " VALUES ('p1', ?, 1, 7)", (MAC,)
    )
    sent = sync.push(mac, pg, MAC)
    assert sent.rows["thread"] == 2

    again = sync.push(mac, pg, MAC)
    assert again.rows == sent.rows
    assert pg.execute("SELECT count(*) FROM thread").fetchone()[0] == 2, "no duplicates"

    other = fresh(tmp_path, "other")
    sync.pull(other, pg)
    assert counts(other) == counts(mac)
    other.close()


@needs_pg
def test_the_pin_rule_holds_on_postgres_too(pg, mac):
    """The CASE expression is the one piece of hand-written cross-engine SQL."""
    add_group(mac, "g-manual", "Client Work")
    sync.push(mac, pg, MAC)
    pg.execute(
        "UPDATE project SET group_id = 'g-manual', group_pinned = 1 WHERE project_id = 'p1'"
    )
    mac.execute("UPDATE project SET group_id = NULL, group_pinned = 0 WHERE project_id = 'p1'")

    sync.push(mac, pg, MAC)

    row = pg.execute(
        "SELECT group_id, group_pinned FROM project WHERE project_id = 'p1'"
    ).fetchone()
    assert (row[0], row[1]) == ("g-manual", 1)
