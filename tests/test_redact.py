"""What may leave this machine. See docs/REDACTION.md.

The load-bearing test in this file is `test_publishing_a_project_id_publishes_
the_path`: it asserts the *attack*, not the defence. Everything else in the
design follows from it being true, so if someone ever "optimises" publication
by shipping the ids, that test is what says no.
"""

from __future__ import annotations

import sqlite3

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


def test_every_path_bearing_column_is_private():
    """The columns that carry a filesystem path, enumerated so a rename is loud."""
    for table, column in (
        ("project", "root_path"), ("project", "project_id"), ("project", "name"),
        ("session", "cwd"), ("ingest_file", "path"),
        ("project_probe", "git_common_dir"), ("project_group", "match_key"),
    ):
        assert redact.verdict(table, column).verdict == redact.PRIVATE, f"{table}.{column}"


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


def test_sync_and_publication_are_different_pipes():
    """`cci sync` moves paths between YOUR machines. This one never does.

    Conflating them is the mistake the whole module exists to prevent, so the
    difference is asserted rather than left to a reader's care.
    """
    from cc_insights import sync

    synced = {c for t in sync.TABLES for c in t.columns}
    assert "root_path" in synced and "cwd" in synced

    published = {f.column for f in redact.FIELDS if f.verdict == redact.PUBLIC}
    assert "root_path" not in published and "cwd" not in published
