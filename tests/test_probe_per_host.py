"""The probe cache belongs to a machine (migration 004).

`project_id = hash(root_path)`, so two machines with the same layout are ONE
project row -- a laptop and a desktop both at `/Users/you/Coding/X`, or two CI
boxes at `/home/ci/work`. Before 004 the probe columns lived on that shared
row, so whichever machine ran `cci group auto` last overwrote the other's
answer. The visible symptom: delete a checkout on the laptop and the desktop's
dashboard marks the project you are working in right now `(gone)`.

Everything below is about who owns which answer.
"""

from __future__ import annotations

import sqlite3

import pytest

from cc_insights import db, grouping, ids, metrics

LAPTOP = "host-laptop"
DESKTOP = "host-desktop"
SHARED = "/Users/you/Coding/Shared"


# ------------------------------------------------------------------ helpers --


def add_host(conn: sqlite3.Connection, host_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, ?, 'test', 0, 0)",
        (host_id, host_id),
    )


def add_project(conn: sqlite3.Connection, root_path: str, name: str | None = None) -> str:
    pid = ids.project_id(root_path)
    conn.execute(
        "INSERT INTO project (project_id, root_path, name) VALUES (?, ?, ?)",
        (pid, root_path, name or root_path.rsplit("/", 1)[-1]),
    )
    return pid


def add_probe(
    conn: sqlite3.Connection,
    project_id: str,
    host_id: str,
    *,
    remote: str | None = None,
    common_dir: str | None = None,
    exists: int | None = None,
    detected_at: int | None = 0,
) -> None:
    add_host(conn, host_id)
    conn.execute(
        """INSERT INTO project_probe
           (project_id, host_id, git_remote, git_common_dir, path_exists, detected_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (project_id, host_id, remote, common_dir, exists, detected_at),
    )


def add_time(conn: sqlite3.Connection, project_id: str, host_id: str, ms: int = 3_600_000) -> None:
    """One session/thread/span, so the metrics endpoints have something to sum."""
    add_host(conn, host_id)
    sid = ids.make_id(project_id, host_id, "session")
    tid = ids.make_id(sid, "thread")
    conn.execute(
        """INSERT INTO session (id, native_id, source, host_id, project_id, started_at, ended_at)
           VALUES (?, ?, 'claude_code', ?, ?, 0, ?)""",
        (sid, sid, host_id, project_id, ms),
    )
    conn.execute(
        "INSERT INTO thread (id, native_id, session_id, started_at, ended_at) VALUES (?, ?, ?, 0, ?)",
        (tid, tid, sid, ms),
    )
    conn.execute(
        """INSERT INTO span (id, session_id, thread_id, started_at, ended_at, event_count)
           VALUES (?, ?, ?, 0, ?, 2)""",
        (ids.make_id(tid, "span"), sid, tid, ms),
    )


# ------------------------------------------------- one project, two machines --


def test_two_machines_keep_separate_answers_for_one_project(conn):
    """The row that could not exist before 004."""
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, LAPTOP, exists=0, remote="https://github.com/o/shared.git")
    add_probe(conn, pid, DESKTOP, exists=1, remote="https://github.com/o/shared.git")

    rows = conn.execute(
        "SELECT host_id, path_exists FROM project_probe WHERE project_id = ? ORDER BY host_id",
        (pid,),
    ).fetchall()
    assert [(r["host_id"], r["path_exists"]) for r in rows] == [(DESKTOP, 1), (LAPTOP, 0)]


def test_a_path_live_on_any_machine_is_not_gone(conn):
    """Deleting a checkout on the laptop must not mark the desktop's work gone.

    `(gone)` next to a project someone is working in right now is the failure
    that made this table necessary, and it is worse than a stale `(gone)`
    elsewhere: it tells the person who is right that they are wrong.
    """
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, LAPTOP, exists=0)
    add_probe(conn, pid, DESKTOP, exists=1)
    add_time(conn, pid, DESKTOP)

    out = metrics.projects(conn)["projects"]
    assert [r["pathExists"] for r in out] == [True]

    members = [m for v in grouping.list_groups(conn) for m in v.members]
    assert [m.path_exists for m in members] == [1]


def test_gone_everywhere_is_still_gone(conn):
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, LAPTOP, exists=0)
    add_probe(conn, pid, DESKTOP, exists=0)
    add_time(conn, pid, DESKTOP)

    assert metrics.projects(conn)["projects"][0]["pathExists"] is False


def test_never_probed_stays_distinct_from_gone(conn):
    """Tri-state survives the aggregate: MAX of no rows is NULL, not 0."""
    pid = add_project(conn, SHARED)
    add_time(conn, pid, DESKTOP)

    assert metrics.projects(conn)["projects"][0]["pathExists"] is None


def test_the_roster_stays_one_row_per_project(conn):
    """A correlated subquery, not a join -- three probes must not triple the row."""
    pid = add_project(conn, SHARED)
    for host in (LAPTOP, DESKTOP, "host-ci"):
        add_probe(conn, pid, host, exists=1)
    add_time(conn, pid, DESKTOP)

    assert len(metrics.projects(conn)["projects"]) == 1
    assert len(grouping.list_groups(conn)) == 1


# ------------------------------------------------------- resolving for a host --


def test_the_ladder_prefers_this_machines_answer(conn):
    """`git_common_dir` describes a disk; the disk we can act on is ours."""
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, LAPTOP, common_dir="/Users/you/Coding/Shared/.git", detected_at=10)
    add_probe(conn, pid, DESKTOP, common_dir="/elsewhere/Shared/.git", detected_at=99)

    [laptop_view] = [p for p in grouping.load_projects(conn, LAPTOP) if p.project_id == pid]
    [desktop_view] = [p for p in grouping.load_projects(conn, DESKTOP) if p.project_id == pid]

    assert laptop_view.git_common_dir == "/Users/you/Coding/Shared/.git"
    assert desktop_view.git_common_dir == "/elsewhere/Shared/.git"


def test_another_machines_answer_is_better_than_none(conn):
    """`git_remote` is a fact about the repo, so a colleague's answer stands in.

    This is what lets rule 1 group a project the local machine has never
    checked out -- the case that only exists once a database holds two hosts.
    """
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, DESKTOP, remote="https://github.com/o/shared.git")

    [seen_from_laptop] = [p for p in grouping.load_projects(conn, LAPTOP) if p.project_id == pid]
    assert seen_from_laptop.git_remote == "https://github.com/o/shared.git"


def test_the_most_recent_probe_wins_among_other_machines(conn):
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, DESKTOP, remote="https://github.com/o/old.git", detected_at=10)
    add_probe(conn, pid, "host-ci", remote="https://github.com/o/new.git", detected_at=99)

    [p] = [p for p in grouping.load_projects(conn, LAPTOP) if p.project_id == pid]
    assert p.git_remote == "https://github.com/o/new.git"


def test_resolution_does_not_depend_on_row_order(conn):
    """Equal detected_at must still give one deterministic answer."""
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, "host-b", remote="https://github.com/o/b.git", detected_at=5)
    add_probe(conn, pid, "host-a", remote="https://github.com/o/a.git", detected_at=5)

    answers = {grouping.load_probes(conn, LAPTOP)[pid]["host_id"] for _ in range(5)}
    assert answers == {"host-a"}, "ties break on host_id, not on insertion order"


def test_a_null_detected_at_never_outranks_a_real_one(conn):
    """SQLite sorts NULLs first under DESC and PostgreSQL sorts them last.

    Without the coalesce this test passes on one engine and fails on the other,
    which is precisely the kind of drift the portability contract exists to
    stop.
    """
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, "host-b", remote="https://github.com/o/unprobed.git", detected_at=None)
    add_probe(conn, pid, "host-a", remote="https://github.com/o/real.git", detected_at=42)

    [p] = [p for p in grouping.load_projects(conn, LAPTOP) if p.project_id == pid]
    assert p.git_remote == "https://github.com/o/real.git"


# --------------------------------------------------------------- writing back --


def test_a_probe_is_filed_under_the_machine_that_ran_it(conn, tmp_path):
    gone = str(tmp_path / "vanished")
    pid = add_project(conn, gone)
    add_host(conn, LAPTOP)
    add_host(conn, DESKTOP)

    grouping.detect(conn, host_id=LAPTOP, probe_fs=True)

    rows = conn.execute("SELECT host_id, path_exists FROM project_probe").fetchall()
    assert [(r["host_id"], r["path_exists"]) for r in rows] == [(LAPTOP, 0)]
    assert pid  # the desktop's row is absent, not zeroed


def test_a_probe_never_launders_another_machines_remote_into_its_own_row(conn, tmp_path):
    """`load_probes` hands the ladder a borrowed remote. The write must not keep it.

    If it did, the desktop's first-hand answer would be indistinguishable from
    the laptop's hearsay copy, and `load_probes`'s preference for the local
    host would start returning a stale value that looks first-hand.
    """
    gone = str(tmp_path / "vanished")
    pid = add_project(conn, gone)
    add_probe(conn, pid, DESKTOP, remote="https://github.com/o/borrowed.git")
    add_host(conn, LAPTOP)

    # The ladder sees the desktop's remote (there is nothing else to go on)...
    [seen] = [p for p in grouping.load_projects(conn, LAPTOP) if p.project_id == pid]
    assert seen.git_remote == "https://github.com/o/borrowed.git"

    # ...but the laptop's own row records only what the laptop learned: nothing.
    grouping.detect(conn, host_id=LAPTOP, probe_fs=True)
    row = conn.execute(
        "SELECT * FROM project_probe WHERE project_id = ? AND host_id = ?", (pid, LAPTOP)
    ).fetchone()
    assert row["path_exists"] == 0
    assert row["git_remote"] is None


def test_probing_refuses_to_guess_which_machine_it_is(conn):
    """Two hosts and no host_id: filing the probe under either one is a lie."""
    add_project(conn, SHARED)
    add_host(conn, LAPTOP)
    add_host(conn, DESKTOP)

    with pytest.raises(RuntimeError, match="host_id"):
        grouping.detect(conn, probe_fs=True)


def test_one_host_needs_no_ceremony(conn, tmp_path):
    """Every database that exists today has exactly one host. It keeps working."""
    add_project(conn, str(tmp_path / "vanished"))
    add_host(conn, LAPTOP)

    grouping.detect(conn, probe_fs=True)

    assert conn.execute("SELECT host_id FROM project_probe").fetchone()[0] == LAPTOP


def test_the_hermetic_mode_needs_no_host_at_all(conn):
    """`probe_fs=False` touches no disk, so it has no machine to name."""
    pid = add_project(conn, SHARED)
    add_probe(conn, pid, DESKTOP, remote="https://github.com/o/shared.git")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 1


# ------------------------------------------------------------------ migration --


def test_migration_004_moves_an_existing_cache_to_its_host(tmp_path):
    """The upgrade path for every database in the field today."""
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn, only_through=2)
    conn.execute(
        "INSERT INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, 'box', 'test', 0, 0)",
        (LAPTOP,),
    )
    pid = add_project(conn, SHARED)
    conn.execute(
        """UPDATE project SET git_remote = ?, git_common_dir = ?, path_exists = 1,
                              detected_at = 7 WHERE project_id = ?""",
        ("https://github.com/o/shared.git", "/Users/you/Coding/Shared/.git", pid),
    )

    db.migrate(conn)

    row = conn.execute("SELECT * FROM project_probe").fetchone()
    assert row["project_id"] == pid
    assert row["host_id"] == LAPTOP
    assert row["git_remote"] == "https://github.com/o/shared.git"
    assert row["git_common_dir"] == "/Users/you/Coding/Shared/.git"
    assert (row["path_exists"], row["detected_at"]) == (1, 7)
    conn.close()


def test_migration_004_leaves_an_unprobed_project_alone(tmp_path):
    """A NULL cache is not a probe result; inventing a row would claim it was."""
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn, only_through=2)
    conn.execute(
        "INSERT INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, 'box', 'test', 0, 0)",
        (LAPTOP,),
    )
    add_project(conn, SHARED)

    db.migrate(conn)

    assert conn.execute("SELECT count(*) FROM project_probe").fetchone()[0] == 0
    conn.close()


def test_migration_004_refuses_to_attribute_a_cache_it_cannot_place(tmp_path):
    """With two hosts there is no honest answer, so re-probe beats guessing.

    Cannot happen today -- sync does not ship yet -- but a wrong attribution
    here is invisible and permanent, while a missing one costs one `cci group
    auto`.
    """
    conn = db.connect(tmp_path / "old.db")
    db.migrate(conn, only_through=2)
    for host in (LAPTOP, DESKTOP):
        conn.execute(
            "INSERT INTO host (host_id, hostname, os, first_seen, last_seen)"
            " VALUES (?, ?, 'test', 0, 0)",
            (host, host),
        )
    pid = add_project(conn, SHARED)
    conn.execute("UPDATE project SET path_exists = 1 WHERE project_id = ?", (pid,))

    db.migrate(conn)

    assert conn.execute("SELECT count(*) FROM project_probe").fetchone()[0] == 0
    conn.close()
