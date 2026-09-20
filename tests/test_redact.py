"""What may leave this machine. See docs/REDACTION.md.

The load-bearing test in this file is `test_publishing_a_project_id_publishes_
the_path`: it asserts the *attack*, not the defence. Everything else in the
design follows from it being true, so if someone ever "optimises" publication
by shipping the ids, that test is what says no.
"""

from __future__ import annotations

import dataclasses
import re
import sqlite3
import typing

import pytest

from cc_insights import ids, redact

REMOTE = "https://github.com/acme/widget"
PRIVATE_PATH = "/Users/alice/Coding/BigClientCorp-nda"


# ------------------------------------------------------------------ helpers --


def add_host(conn, host_id="h1", hostname="alice-macbook"):
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, ?, 'Darwin', 0, 0)",
        (host_id, hostname),
    )
    return host_id


def add_group(conn, gid, name, *, remote=None, origin="git_remote"):
    conn.execute(
        """INSERT INTO project_group (group_id, name, origin, match_key, remote_url,
                                      forge, owner, repo, web_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'github', 'acme', ?, ?, 0, 0)""",
        (gid, name, origin, remote or name, remote, name, remote),
    )
    return gid


def add_project(conn, root_path, *, group_id=None):
    pid = ids.project_id(root_path)
    conn.execute(
        "INSERT INTO project (project_id, root_path, name, group_id) VALUES (?, ?, ?, ?)",
        (pid, root_path, root_path.rsplit("/", 1)[-1], group_id),
    )
    return pid


def add_work(conn, host_id, project_id, sid, *, ms=3_600_000, branch=None,
             subagent=0, attended=1, cwd=None):
    conn.execute(
        """INSERT INTO session (id, native_id, source, host_id, project_id, cwd,
                                git_branch, cli_version, started_at, ended_at,
                                active_ms, event_count)
           VALUES (?, ?, 'claude_code', ?, ?, ?, ?, '2.0.0', 0, ?, ?, 4)""",
        (sid, sid, host_id, project_id, cwd, branch, ms, ms),
    )
    tid = ids.make_id(sid, "t")
    conn.execute(
        """INSERT INTO thread (id, native_id, session_id, is_subagent, started_at, ended_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (tid, tid, sid, subagent, ms),
    )
    conn.execute(
        """INSERT INTO span (id, session_id, thread_id, started_at, ended_at,
                             event_count, attended)
           VALUES (?, ?, ?, 0, ?, 4, ?)""",
        (ids.make_id(tid, "sp"), sid, tid, ms, attended),
    )


@pytest.fixture
def corpus(conn):
    """One repo-backed project and one private local-only one."""
    host = add_host(conn)
    gid = add_group(conn, "g-widget", "widget", remote=REMOTE)
    public = add_project(conn, "/Users/alice/Coding/widget", group_id=gid)
    private = add_project(conn, PRIVATE_PATH)
    add_work(conn, host, public, "s-public", ms=7_200_000, branch="feat/checkout",
             cwd="/Users/alice/Coding/widget")
    add_work(conn, host, private, "s-private", ms=1_800_000, cwd=PRIVATE_PATH)
    return conn


# ------------------------------------------------- the premise of the design --


def test_publishing_a_project_id_publishes_the_path(conn):
    """THE finding. project_id = sha256(root_path), so a guess confirms itself.

    Measured on the author's corpus: 555 guesses built from a username, eight
    conventional directory names and the repo names in the remotes recovered
    20% of the project ids outright (docs/probes/leakage.py). Which is why
    `project_id` is PRIVATE and publication re-keys on the remote instead.
    """
    published_id = add_project(conn, PRIVATE_PATH)

    # A colleague knows the username and tries the obvious shapes.
    for guess in (
        "/Users/alice/Coding/BigClientCorp-nda",
        "/Users/alice/src/BigClientCorp-nda",
        "/Users/alice/Coding/something-else",
    ):
        if ids.project_id(guess) == published_id:
            assert guess == PRIVATE_PATH
            break
    else:
        pytest.fail("the attack this whole design is built around did not reproduce")

    assert redact.verdict("project", "project_id").verdict == redact.PRIVATE
    assert redact.verdict("project", "root_path").verdict == redact.PRIVATE


def test_repo_id_is_not_vulnerable_to_the_same_attack(conn):
    """It hashes the remote, which the viewer already has. Nothing to confirm."""
    assert redact.repo_id(REMOTE) == redact.repo_id(REMOTE)
    assert redact.repo_id(REMOTE) != redact.repo_id(REMOTE + "-other")


def test_two_spellings_of_one_remote_publish_as_one_repo():
    """ssh and https checkouts of a repo are the same repo to the team view."""
    from cc_insights import grouping

    ssh = grouping.normalize_remote("git@github.com:acme/widget.git")
    https = grouping.normalize_remote("https://github.com/acme/widget")
    assert redact.repo_id(ssh) == redact.repo_id(https)


# -------------------------------------------------------- the coverage guard --


def test_every_schema_column_is_classified(conn):
    assert redact.unclassified(conn) == []


def test_a_new_column_is_reported_until_someone_rules_on_it(conn):
    """The mechanism behind 'closed by default'.

    A denylist is a list of the leaks someone thought of; the next migration
    adds one that is not on it. This makes the migration answer, in writing,
    at the moment its author still remembers what the column is for.
    """
    conn.execute("ALTER TABLE project ADD COLUMN client_codename TEXT")
    assert ("project", "client_codename") in redact.unclassified(conn)


def test_every_verdict_carries_a_reason_that_stands_alone():
    """Terse is fine -- "a path" says everything. Referential is not.

    A reason like "same", pointing at the line above, is correct until someone
    reorders the table and then quietly is not. Whoever reviews this file next
    has to be able to read any single line and know why.
    """
    placeholders = {"same", "as above", "ditto", "n/a", "tbd", "todo", "see above", ""}
    for f in redact.FIELDS:
        assert f.verdict in (redact.PUBLIC, redact.PRIVATE, redact.DERIVED)
        assert f.why.strip().lower().rstrip(".") not in placeholders, (
            f"{f.table}.{f.column} has a referential reason: {f.why!r}"
        )


def test_no_classification_outlives_the_column_it_describes(conn):
    """The other direction of the coverage guard. See `redact.stale`."""
    assert redact.stale(conn) == []


def test_a_renamed_column_leaves_a_verdict_that_still_answers(conn):
    """Why one direction is half a guard.

    `unclassified` catches the new name. Nothing catches the old one, and the
    old one is the dangerous half: a dropped entry goes quiet, a stale entry
    keeps answering. After a rename, `verdict("session", "cwd")` still returns
    PRIVATE for a column that does not exist, so every by-name check reads a
    reassuring answer about nothing while the live column carries the path.
    """
    conn.execute("ALTER TABLE session RENAME COLUMN cwd TO working_dir")

    # Someone classifies the new name and the suite is green again...
    assert ("session", "working_dir") in redact.unclassified(conn)
    # ...but the entry that nothing removed is still here, still answering.
    assert redact.verdict("session", "cwd").verdict == redact.PRIVATE
    assert ("session", "cwd") in redact.stale(conn)


def test_every_path_bearing_column_is_private(conn):
    """The columns that carry a filesystem path, checked against the live schema.

    The enumeration is worth having -- these are the columns whose leaking IS
    the finding in docs/REDACTION.md -- but an enumeration on its own says
    nothing about the database. The previous version of this test called
    `redact.verdict()` and stopped there, which is a dict lookup against a
    hardcoded list. It would have passed unchanged after a migration renamed
    every column in it, reading seven confident verdicts for seven columns
    that no longer existed. Its docstring claimed "enumerated so a rename is
    loud"; a rename was silent.

    Each name is now asserted to still BE a column before its verdict is
    trusted, so the rename fails here, by name, next to the list that needs
    editing.
    """
    live = redact.schema_columns(conn)
    for table, column in (
        ("project", "root_path"), ("project", "project_id"), ("project", "name"),
        ("session", "cwd"), ("ingest_file", "path"),
        ("project_probe", "git_common_dir"), ("project_group", "match_key"),
    ):
        assert (table, column) in live, (
            f"{table}.{column} is not in the schema any more, so the verdict "
            f"below would be read off a dead FIELDS entry. Point this test at "
            f"the new name and delete the old entry from redact.FIELDS."
        )
        assert redact.verdict(table, column).verdict == redact.PRIVATE, f"{table}.{column}"


#: The shapes a filesystem path arrives in when somebody names a column.
_PATH_SHAPED = re.compile(
    r"(^|_)(path|paths|dir|dirs|directory|cwd|root|folder|file|filename|location)(_|$)"
)


def test_a_path_shaped_column_anywhere_in_the_schema_cannot_be_published(conn):
    """A tripwire over every column the database has, not over seven names.

    The enumeration above can only know the columns that existed when it was
    written. This reads the live schema, so migration 007's `working_dir`,
    or a `local_root`, or a `checkout_path`, is caught the moment it appears
    -- including the case the enumeration cannot catch at all, where somebody
    keeps the list green by classifying the new column PUBLIC.

    It matches on names, which is the denylist shape `redact.py` condemns, and
    it is deliberately NOT the guard. `unclassified` is the guard and it is
    closed by default. This can only ever ADD failures on top of it, so the
    objection to denylists -- that whatever is not listed is permitted --
    does not apply here: a path column this pattern has never heard of is
    still withheld until a human rules on it. What this buys is that the
    obvious names cannot be waved through by a tired reviewer.

    DERIVED is allowed and PUBLIC is not: a path-shaped column may be
    replaced by something computed, which is how `project.group_id` becomes a
    repo_id, but it may never be copied across as itself.
    """
    matched = []
    for table, column in sorted(redact.schema_columns(conn)):
        if not _PATH_SHAPED.search(column):
            continue
        matched.append(f"{table}.{column}")
        f = redact.verdict(table, column)
        assert f is not None, f"{table}.{column} is unclassified"
        assert f.verdict != redact.PUBLIC, (
            f"{table}.{column} is named like a filesystem path and is classified "
            f"PUBLIC ({f.why!r}). Publishing a path, or anything derived from "
            f"one, is the finding docs/REDACTION.md is about."
        )
    assert matched, "the pattern matched nothing at all, so this test proved nothing"


# ------------------------------------------------------------- the boundary --


def test_only_repo_backed_work_crosses(corpus):
    pub = redact.publication(corpus, "alice")
    assert [r.remote_url for r in pub.repos] == [REMOTE]
    assert [s.session_id for s in pub.sessions] == ["s-public"]


def test_work_with_no_remote_is_withheld_and_counted(corpus):
    """Not silently dropped: a view that omits your time is wrong, not private."""
    pub = redact.publication(corpus, "alice")
    assert pub.published_ms == 7_200_000
    assert pub.withheld_ms == 1_800_000
    assert pub.withheld_projects == 1


def test_published_plus_withheld_is_the_whole_truth(corpus):
    """The two numbers must partition, with nothing invented and nothing lost."""
    total = corpus.execute("SELECT sum(ended_at - started_at) FROM span").fetchone()[0]
    pub = redact.publication(corpus, "alice")
    assert pub.published_ms + pub.withheld_ms == total


def test_a_session_with_no_project_still_counts_as_withheld(corpus):
    """It belongs to no project row, so summing the failures would lose it.

    This is why withheld is total-minus-published rather than a sum over the
    projects that failed the test.
    """
    host = add_host(corpus)
    add_work(corpus, host, None, "s-orphan", ms=600_000)

    pub = redact.publication(corpus, "alice")
    total = corpus.execute("SELECT sum(ended_at - started_at) FROM span").fetchone()[0]
    assert pub.published_ms + pub.withheld_ms == total
    assert pub.withheld_ms == 1_800_000 + 600_000


def test_a_group_with_no_remote_cannot_carry_work_across(conn):
    """Rules 2-4 group by path, so their match_key is a path. Not a boundary."""
    host = add_host(conn)
    gid = add_group(conn, "g-local", "scratch", remote=None, origin="path_ancestor")
    pid = add_project(conn, "/Users/alice/scratch", group_id=gid)
    add_work(conn, host, pid, "s1")

    pub = redact.publication(conn, "alice")
    assert pub.repos == []
    assert pub.sessions == []
    assert pub.published_ms == 0


def test_credentials_are_stripped_even_if_the_stored_row_has_them(conn):
    """The remote is re-normalized at publication, never trusted from the row.

    A value stored before a fix to `normalize_remote` must not be the thing
    that ships.
    """
    add_group(conn, "g", "monorepo", remote="https://alice@dev.azure.com/org/proj/_git/repo")
    pub = redact.publication(conn, "alice")
    assert "@" not in pub.repos[0].remote_url
    assert "alice" not in pub.repos[0].remote_url


# ------------------------------------- the projection is tied to the table --
#
# `publication()` is hand-written SELECTs building hand-written dataclasses.
# Nothing in it reads `FIELDS`, so until these tests existed, classifying a
# column PRIVATE did not stop it being emitted and no test compared the two at
# all -- a PRIVATE column added to the projection failed nothing. Two
# independent checks close that: `redact.PROVENANCE` declares what is allowed,
# and the marker sweep at the bottom observes what actually came out.


def _sources() -> set[tuple[str, str]]:
    return {
        src
        for per_field in redact.PROVENANCE.values()
        for origin in per_field.values()
        for src in origin.sources
    }


def test_the_projection_carries_exactly_the_row_types_provenance_knows_about():
    """A fourth published row type must be ruled on, not just added.

    Read off `Publication`'s own annotations rather than a list here, because
    a list here is a copy, and `sync.py`'s first schema test proved what a
    copy is worth: it compared the module to a hardcoded duplicate of itself
    and sailed through a merge that added three tables.
    """
    carried = set()
    for hint in typing.get_type_hints(redact.Publication).values():
        if typing.get_origin(hint) is list:
            (element,) = typing.get_args(hint)
            carried.add(element)
    assert carried == set(redact.PROVENANCE)


def test_every_published_field_says_where_it_came_from():
    """Adding a field to the projection is adding a field to the export."""
    for cls, declared in redact.PROVENANCE.items():
        actual = {f.name for f in dataclasses.fields(cls)}
        assert set(declared) == actual, (
            f"{cls.__name__}: {sorted(actual ^ set(declared))} is in one of the "
            f"dataclass and redact.PROVENANCE but not the other. A published "
            f"field with no declared source is a field nobody has ruled on."
        )


def test_no_published_field_is_sourced_from_a_private_column():
    """The check that was missing: FIELDS compared against what is emitted.

    A straight copy must come from a PUBLIC column -- there is nothing else a
    copy can honestly be. A computed field may additionally read a DERIVED
    one, because "replaced by something computed" is exactly what DERIVED
    means, and `repo_id` reading `project.group_id` is that working. Neither
    may touch a PRIVATE column, which is the whole rule in one line.
    """
    for cls, declared in redact.PROVENANCE.items():
        for name, origin in declared.items():
            allowed = (
                (redact.PUBLIC, redact.DERIVED) if origin.why else (redact.PUBLIC,)
            )
            for table, column in origin.sources:
                f = redact.verdict(table, column)
                assert f is not None, f"{cls.__name__}.{name}: {table}.{column} is unclassified"
                assert f.verdict in allowed, (
                    f"{cls.__name__}.{name} is published from {table}.{column}, "
                    f"which is classified {f.verdict} ({f.why!r})."
                )


def test_a_computed_field_has_to_justify_itself_and_a_copy_does_not():
    """`why` is what separates the two rules above, so it cannot be decoration.

    A one-word `why` on a field sourced from a DERIVED column is how the
    PRIVATE-adjacent path gets unlocked, so it has to be an argument somebody
    can disagree with -- the same bar `test_every_verdict_carries_a_reason_
    that_stands_alone` sets for FIELDS, for the same reason.
    """
    for cls, declared in redact.PROVENANCE.items():
        for name, origin in declared.items():
            if not origin.sources:
                assert origin.why, f"{cls.__name__}.{name} claims no source and no reason"
            if origin.why:
                assert len(origin.why) > 25, (
                    f"{cls.__name__}.{name} is computed but its reason is too "
                    f"short to argue with: {origin.why!r}"
                )
            else:
                assert len(origin.sources) == 1, (
                    f"{cls.__name__}.{name} has {len(origin.sources)} sources but "
                    f"no reason, so it is not a straight copy of any of them"
                )


def test_no_provenance_entry_points_at_a_column_that_is_gone(conn):
    """The same rename that rots FIELDS rots this, and here it is worse.

    A stale FIELDS entry misleads a reader. A stale PROVENANCE entry also
    holds the door open: the verdict it is checked against belongs to a
    column nobody is writing to any more, while the value actually being
    published comes from wherever the SELECT was repointed.
    """
    live = redact.schema_columns(conn)
    assert _sources() <= live, sorted(_sources() - live)


def _mark_every_private_value(conn: sqlite3.Connection) -> list[str]:
    """Prefix every PRIVATE value in the database with a traceable marker.

    Prefixing rather than overwriting, because several PRIVATE columns are
    join keys -- `project.project_id` above all, which is the §0 attack
    itself. Overwriting them would break the joins `publication()` runs on
    and leave the test passing over an empty projection, which is the shape
    of a test that checks nothing. A prefix keeps every value distinct and
    every join intact, and foreign keys are followed from
    `PRAGMA foreign_key_list` so parent and child get the same prefix.

    Driven off `FIELDS` and the live schema, so a PRIVATE column added by a
    future migration is swept in without anybody editing this.
    """
    private = {(f.table, f.column) for f in redact.FIELDS if f.verdict == redact.PRIVATE}
    live = redact.schema_columns(conn)
    tables = sorted({t for t, _ in live})

    # Text-affinity columns only. A marker is a string, and a column that
    # cannot hold a string cannot hold a path, a username or a directory name
    # -- which is what this sweep is looking for. `schema_migrations.version`
    # is the one that makes this explicit rather than tidy: it is an INTEGER
    # PRIMARY KEY, and writing text to a rowid alias is a datatype mismatch,
    # not a leak. PRIVATE counters and timestamps are covered by PROVENANCE
    # instead, which does not care what a column can hold.
    texty = {
        (table, r[1])
        for table in tables
        for r in conn.execute(f"PRAGMA table_info({table})")
        if any(k in (r[2] or "").upper() for k in ("CHAR", "CLOB", "TEXT"))
    }

    marks: dict[tuple[str, str], str] = {}
    for table, column in sorted(private & live & texty):
        marks[(table, column)] = f"MARK_{table}_{column}_"

    for table in tables:
        for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
            parent, child_col, parent_col = row[2], row[3], row[4]
            if parent_col is None:  # implicit reference to the parent's PK
                parent_col = next(
                    (r[1] for r in conn.execute(f"PRAGMA table_info({parent})") if r[5]),
                    None,
                )
            mark = marks.get((parent, parent_col))
            if mark and (table, child_col) not in marks:
                marks[(table, child_col)] = mark

    # One transaction with the references deferred, because rewriting a key
    # necessarily breaks it for as long as its children still hold the old
    # value. Deferred rather than switched off: the constraints are checked
    # at COMMIT, so if the fixup above missed a reference this raises here
    # instead of leaving the test running over a quietly orphaned corpus.
    applied: list[str] = []
    conn.execute("BEGIN")
    conn.execute("PRAGMA defer_foreign_keys = ON")
    for (table, column), mark in sorted(marks.items()):
        changed = conn.execute(
            f"UPDATE {table} SET {column} = ? || {column} WHERE {column} IS NOT NULL",
            (mark,),
        ).rowcount
        if changed:
            applied.append(mark)
    conn.execute("COMMIT")
    return applied


def test_no_private_column_reaches_the_projection(corpus):
    """What `publication()` emitted, not what the table says it may emit.

    `PROVENANCE` is a declaration, and a declaration can be wrong about which
    SELECT a value really came from. This asks the other question: every
    PRIVATE value in the database is given a marker, the projection is built,
    and no marker may appear in it. A hand-written SELECT that starts reading
    `session.cwd` fails here whatever anyone wrote down.

    The three assertions at the end are the point of the test as much as the
    sweep is: a marker sweep over an empty projection passes perfectly.
    """
    applied = _mark_every_private_value(corpus)
    assert applied, "no PRIVATE value was marked, so nothing was actually tested"

    pub = redact.publication(corpus, "alice")
    assert pub.repos and pub.sessions and pub.spans, "the projection came out empty"

    blob = repr(pub)
    leaked = sorted({mark for mark in applied if mark in blob})
    assert not leaked, (
        f"the projection carries value(s) from PRIVATE column(s): {leaked}. "
        f"Either the column is not actually private -- change the verdict in "
        f"redact.FIELDS and say why -- or publication() must stop selecting it."
    )


# ---------------------------------------------------------- the projection --


def test_no_path_reaches_the_projection(corpus):
    pub = redact.publication(corpus, "alice")
    blob = repr(pub)
    for forbidden in (PRIVATE_PATH, "/Users/alice", "alice-macbook", "BigClientCorp"):
        assert forbidden not in blob, forbidden


def test_the_local_host_id_is_replaced_by_the_actor(corpus):
    pub = redact.publication(corpus, "alice@example.com")
    assert all(s.actor == "alice@example.com" for s in pub.sessions)
    assert "h1" not in repr(pub)
    assert redact.verdict("session", "host_id").verdict == redact.DERIVED


def test_thread_role_partitions_exactly_as_stats_does():
    """The three buckets stats.py reports. A fourth would break every total."""
    assert redact.thread_role(is_subagent=0, attended=1) == redact.HUMAN
    assert redact.thread_role(is_subagent=1, attended=1) == redact.AUTONOMOUS
    assert redact.thread_role(is_subagent=1, attended=None) == redact.AUTONOMOUS
    assert redact.thread_role(is_subagent=0, attended=0) == redact.UNATTENDED
    assert redact.thread_role(is_subagent=0, attended=None) == redact.UNATTENDED


def test_spans_carry_their_role_across(conn):
    host = add_host(conn)
    gid = add_group(conn, "g", "widget", remote=REMOTE)
    pid = add_project(conn, "/Users/alice/Coding/widget", group_id=gid)
    add_work(conn, host, pid, "s-human", attended=1)
    add_work(conn, host, pid, "s-agent", subagent=1)

    roles = {s.thread_role for s in redact.publication(conn, "alice").spans}
    assert roles == {redact.HUMAN, redact.AUTONOMOUS}


def test_scoping_to_one_host_publishes_only_that_machine(corpus):
    other = add_host(corpus, "h2", "bob-pc")
    gid = "g-widget"
    pid = add_project(corpus, "/Users/bob/code/widget", group_id=gid)
    add_work(corpus, other, pid, "s-bob")

    mine = redact.publication(corpus, "alice", host_id="h1")
    assert [s.session_id for s in mine.sessions] == ["s-public"]


# ---------------------------------------------------------------- the audit --


def test_a_clean_projection_audits_clean(corpus):
    pub = redact.publication(corpus, "alice")
    assert redact.audit(pub, redact.local_secrets(corpus)).clean


def test_a_path_that_slipped_through_is_a_blocking_leak(corpus):
    pub = redact.publication(corpus, "alice")
    pub.sessions[0] = redact.Session(
        **{**vars(pub.sessions[0]), "git_branch": f"wip{PRIVATE_PATH}"}
    )
    result = redact.audit(pub, redact.local_secrets(corpus))
    assert not result.clean
    assert any("local path" in line for line in result.leaks)


def test_a_path_derived_id_that_slipped_through_is_a_blocking_leak(corpus):
    """The §0 attack arriving through a field that looks innocent."""
    pub = redact.publication(corpus, "alice")
    leaked = ids.project_id(PRIVATE_PATH)
    pub.sessions[0] = redact.Session(**{**vars(pub.sessions[0]), "git_branch": leaked})

    result = redact.audit(pub, redact.local_secrets(corpus))
    assert not result.clean


def test_the_username_is_a_leak_in_a_path_but_not_in_a_repo_owner(conn):
    """The false positive that broke the first version of this audit.

    `alice` is a path segment in /Users/alice AND the GitHub owner in
    https://github.com/alice/thing. Flagging the second would be noise, and an
    audit people learn to ignore is not an audit.
    """
    host = add_host(conn, hostname="box")
    add_group(conn, "g", "thing", remote="https://github.com/alice/thing")
    pid = add_project(conn, "/Users/alice/Coding/thing", group_id="g")
    add_work(conn, host, pid, "s1")

    pub = redact.publication(conn, "alice")
    result = redact.audit(pub, redact.Secrets(paths=(), identity=("alice",), names=()))
    assert result.clean, result.leaks


def test_a_word_collision_is_a_warning_not_a_blocker(corpus):
    """Ordinary directory names are ordinary words, and they collide.

    On the author's corpus `~/Coding/CC-Insights/frontend` is withheld and the
    published branch `t3code/frontend-chat-performance` contains `frontend`.
    Nothing leaked. Blocking on that would train people to skip the check.
    """
    pub = redact.publication(corpus, "alice")
    pub.sessions[0] = redact.Session(
        **{**vars(pub.sessions[0]), "git_branch": "feat/dashboard-rewrite"}
    )
    result = redact.audit(
        pub, redact.Secrets(paths=(), identity=(), names=("dashboard",))
    )
    assert result.clean, "a shared word must not block publication"
    assert result.warnings, "but it should still be shown to a human"


def test_the_hostname_is_a_blocking_leak(corpus):
    pub = redact.publication(corpus, "alice")
    pub.sessions[0] = redact.Session(
        **{**vars(pub.sessions[0]), "git_branch": "alice-macbook"}
    )
    result = redact.audit(pub, redact.local_secrets(corpus))
    assert not result.clean


def test_a_withheld_projects_directory_name_is_watched_but_a_published_one_is_not(corpus):
    """A directory inside a repo you can see is not a secret from you."""
    secrets = redact.local_secrets(corpus)
    assert "BigClientCorp-nda" in secrets.names
    assert "widget" not in secrets.names


# ------------------------------------------------------------------- the cli --


def test_privacy_refuses_while_a_column_is_unclassified(tmp_path, capsys):
    from cc_insights import cli, db

    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    conn = db.connect(tmp_path / "cc-insights.db")
    conn.execute("ALTER TABLE project ADD COLUMN client_codename TEXT")
    conn.close()

    assert cli.main(["--config-dir", str(tmp_path), "privacy"]) == 1
    assert "client_codename" in capsys.readouterr().err


def test_privacy_sends_nothing_and_says_so(tmp_path, capsys):
    from cc_insights import cli

    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(tmp_path), "privacy"]) == 0
    assert "Nothing was sent" in capsys.readouterr().out


def test_sync_and_publication_are_different_pipes(corpus):
    """`cci sync` moves paths between YOUR machines. This one never does.

    Conflating them is the mistake the whole module exists to prevent, so the
    difference is asserted rather than left to a reader's care.

    The earlier version of this collapsed both sides to bare sets of column
    names with the tables thrown away, and then only consulted the
    classification table. Two things wrong with that. A bare name means a
    PUBLIC `cwd` on some *other* table would have satisfied it, and the
    classification table is not what ships -- `publication()` is. Keyed on
    (table, column), and asked of the projection.
    """
    from cc_insights import sync

    synced = {(t.name, c) for t in sync.TABLES for c in t.columns}
    assert ("project", "root_path") in synced, "sync is supposed to move paths"
    assert ("session", "cwd") in synced

    assert ("project", "root_path") not in _sources()
    assert ("session", "cwd") not in _sources()

    pub = redact.publication(corpus, "alice")
    assert pub.sessions, "an empty projection would satisfy anything below"
    blob = repr(pub)
    for (root_path,) in corpus.execute("SELECT root_path FROM project"):
        assert root_path not in blob
    for (cwd,) in corpus.execute("SELECT cwd FROM session WHERE cwd IS NOT NULL"):
        assert cwd not in blob


# ------------------------------------------- where the refusal actually is --
#
# `redact.audit` says the leak list must be empty before anything is sent, and
# for a while nothing anywhere performed that refusal: `publication()` never
# called `audit()`, and the only caller was `cci privacy`, a command that
# prints a report and sends nothing. A refusal a caller skips by not calling
# the reporting function is not a refusal. It lives in the transport now, and
# these are what keep it there.


class _Recorder:
    """A client that fails loudly if the transport ever tries to send."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def team_publish(self, kind, actor, rows):   # pragma: no cover - must not run
        self.calls.append(kind)
        raise AssertionError(f"a projection that failed its audit reached the wire: {kind}")


def test_the_transport_refuses_a_projection_that_failed_its_audit(corpus):
    """And refuses before the first request, not between two of them.

    A half-published projection is worse than none: repos and sessions would
    be readable by a team while the withheld counters that make the totals
    honest never arrive.
    """
    from cc_insights import remote

    pub = redact.publication(corpus, "alice")
    pub.sessions[0] = redact.Session(
        **{**vars(pub.sessions[0]), "git_branch": f"wip{PRIVATE_PATH}"}
    )

    client = _Recorder()
    with pytest.raises(remote.Unsafe):
        remote.publish(corpus, client, pub, host_id="h1")
    assert client.calls == [], "nothing may be sent"


def test_the_transport_refuses_to_publish_over_an_unclassified_column(corpus):
    """Closed by default has to hold at the moment of sending, too.

    An unclassified column means a migration added something nobody has ruled
    on. A publisher that has not read the ruling cannot honour it, so the
    answer is to stop rather than to ship the fields it does recognise.
    """
    from cc_insights import remote

    pub = redact.publication(corpus, "alice")
    corpus.execute("ALTER TABLE project ADD COLUMN client_codename TEXT")

    client = _Recorder()
    with pytest.raises(remote.Unsafe, match="client_codename"):
        remote.publish(corpus, client, pub, host_id="h1")
    assert client.calls == []


def test_a_clean_projection_is_not_refused(corpus):
    """The refusals above are worthless if they also stop the legitimate case."""
    from cc_insights import remote

    pub = redact.publication(corpus, "alice")
    assert remote.check_publishable(corpus, pub).clean
