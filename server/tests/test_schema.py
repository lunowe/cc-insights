"""What the schema itself guarantees, checked against the live catalog.

These do not exercise a code path. They assert properties of the database the
code runs on, which is where several of this server's privacy rules are
actually enforced -- reading them out of `information_schema` is the only way
to know the rule survived the last migration.
"""

from __future__ import annotations

import re

import pytest

from cci_server import personal_schema
from cci_server.repoid import github_remote, repo_id

#: The tables that were the team store when this file was written. NOT the
#: definition -- `team_tables()` is, and it reads the catalog. This is kept
#: only as a floor: every one of these must still be found there, so deleting
#: a team table, or quietly reclassifying one as control plane, is loud.
TEAM_TABLES = (
    "published_repo", "published_session", "published_session_branch",
    "published_span", "published_withheld", "repo_publisher", "team_repo",
)

#: Column names that carry, or are derived from, a filesystem path. Every one
#: is classified PRIVATE in `redact.FIELDS`.
FORBIDDEN_IN_TEAM_STORE = (
    "root_path", "path", "cwd", "project_id", "git_common_dir", "match_key",
    "hostname", "native_id", "native_event_id", "cli_version", "agent_name",
    "group_id", "tool_name", "model",
)

#: Types that cannot hold a path, a name or a paragraph. Anything else counts
#: as free text and has to be declared below.
#:
#: This is the closed-by-default half, and it replaces a filter that asked for
#: `data_type IN ('text', 'character varying')`. Under that filter a `jsonb`,
#: `json`, `bytea`, `xml` or `citext` column was not free text as far as this
#: test was concerned -- each of which stores `/Users/alice/Coding/nda-client`
#: or a whole prompt perfectly happily, and each of which would have passed
#: both team-store checks without comment. Listing what is SAFE means a type
#: nobody here has thought of is free text until somebody argues otherwise,
#: which is the right way round: the cost of being wrong is one line in
#: `allowed`, against a column that can hold anything.
#:
#: `ARRAY` and `USER-DEFINED` are deliberately absent. An array of text is
#: text, and a user-defined type is by definition one this list has never
#: seen.
_CANNOT_HOLD_PROSE = frozenset({
    "bigint", "integer", "smallint", "numeric", "real", "double precision",
    "boolean", "date", "interval", "uuid",
    "timestamp with time zone", "timestamp without time zone",
    "time with time zone", "time without time zone",
})


#: Tables that are neither the personal store nor the team store: who exists,
#: what they may see, and what has been migrated. Nothing a colleague reads
#: agent-time data out of.
#:
#: Each needs a reason, because this dict is the ONLY way a table escapes the
#: team-store checks below -- and "it is not really a team table" is exactly
#: the sentence somebody would write on the way to shipping `published_project`
#: with a path column in it.
CONTROL_PLANE: dict[str, str] = {
    "account": "who exists; no agent-time data and nothing derived from a path",
    "identity": "which forge login proves an account; an OAuth subject, not a repo fact",
    "api_token": "credentials, stored hashed, readable by nobody including their owner",
    "device_authorization": "in-flight device grants, short-lived and single-account",
    "account_repo_access": "the access CACHE that answers 'may this account see this "
                           "repo' -- it holds repo_ids the account already proved it "
                           "can see, which is the boundary itself rather than a thing "
                           "published across it",
    "team": "a team's name, chosen by a human for other humans to read",
    "team_member": "who is in a team and in what role; membership, not measurement",
    "schema_migrations": "which migrations have run on this database",
}


def _columns(conn, table: str) -> list[str]:
    return [
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
    ]


def _live_tables(conn) -> set[str]:
    return {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
    }


def team_tables(conn) -> set[str]:
    """Every live table that is not the personal store and not control plane.

    Derived by subtraction, not listed. `TEAM_TABLES` used to be a hardcoded
    tuple that both team-store checks iterated, which meant a migration
    adding `published_project` was examined by neither of them: not a
    failure, not a warning, simply absent from two tests that looked like
    they covered the schema. A hardcoded list cannot fail to mention a table
    it has never heard of.

    Subtracting inverts the default. A new table is IN the team store, and
    therefore fully checked, until somebody writes a reason in
    `CONTROL_PLANE` -- which is the same shape as
    `test_every_table_is_either_synced_or_excluded_on_purpose` on the client,
    for the same reason.
    """
    return _live_tables(conn) - set(personal_schema.BY_NAME) - set(CONTROL_PLANE)


def test_every_table_is_personal_team_or_control_plane_on_purpose(migrated_db):
    """Nothing in this database escapes all three sets of rules by accident.

    The partition is what makes `team_tables()` trustworthy: a new table is
    checked as a team table, and the only alternative is a written reason.
    The other two assertions keep the escape hatch honest -- a stale
    `CONTROL_PLANE` entry silently un-checks a table that gets re-created
    later under the same name, and a one-word reason is not one.
    """
    with migrated_db.connection() as conn:
        live = _live_tables(conn)
        assert set(CONTROL_PLANE) <= live, (
            f"CONTROL_PLANE names table(s) that no longer exist: "
            f"{sorted(set(CONTROL_PLANE) - live)}. A stale entry here excuses a "
            f"future table of the same name from every check below."
        )
        assert set(personal_schema.BY_NAME) <= live, (
            f"personal_schema names table(s) that no longer exist: "
            f"{sorted(set(personal_schema.BY_NAME) - live)}"
        )
        assert set(TEAM_TABLES) <= team_tables(conn), (
            f"table(s) that used to be in the team store are not there any more: "
            f"{sorted(set(TEAM_TABLES) - team_tables(conn))}. Either they were "
            f"dropped, or something moved them into CONTROL_PLANE and out of "
            f"the reach of the path and free-text checks."
        )
    for table, why in CONTROL_PLANE.items():
        assert len(why) > 25, f"{table} needs a reason someone can argue with"


def test_team_store_has_no_path_column(migrated_db):
    """A path column in the team store is a leak waiting for one careless INSERT.

    docs/REDACTION.md §2: the shared database cannot leak what it never
    received. The enforcement is that there is nowhere to put a path -- not a
    nullable column nobody writes to, which is one bug away from being
    written to and which no code review reliably catches. If this fails,
    somebody added a column that can hold `~/Coding/<client-name>`, and the
    projection stopped being a projection.

    `FORBIDDEN_IN_TEAM_STORE` is a denylist of column names, which is the
    shape `redact.py:60` condemns: it lists the leaks someone thought of, and
    the next migration adds one that is not on it. `working_dir`,
    `local_root` and `checkout_path` all sail past it. It is kept because a
    named column is a clearer failure message than a type mismatch, but it is
    NOT what enforces the rule -- the free-text check below is, and that one
    is closed by default and knows nothing about names.
    """
    with migrated_db.connection() as conn:
        for table in sorted(team_tables(conn)):
            cols = _columns(conn, table)
            assert cols, f"{table} does not exist"
            offending = sorted(set(cols) & set(FORBIDDEN_IN_TEAM_STORE))
            assert not offending, f"{table} has path-derived column(s) {offending}"


def _free_text_columns(conn, table: str) -> dict[str, str]:
    return {
        r["column_name"]: r["data_type"]
        for r in conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        if r["data_type"] not in _CANNOT_HOLD_PROSE
    }


def test_team_store_holds_no_free_text_beyond_the_branch_name(migrated_db):
    """Metadata only. A free-text column is where prompt text ends up.

    The team store's free-text columns are ids, a remote URL, its parsed
    parts, an actor, a source, a thread role and the branch name. If this
    list grows, somebody added somewhere for prose to live, and "no prompt
    text in the schema" is a release blocker in this project rather than a
    style rule.

    An equality check rather than a subset one, and over `team_tables()`
    rather than a hardcoded tuple, so the two ways of getting a path in here
    both fail: adding a column to a table that is already listed, and adding
    a whole table nobody remembered to list.
    """
    allowed = {
        "published_repo": {"repo_id", "remote_url", "forge", "owner", "repo",
                           "web_url", "name"},
        "published_session": {"session_id", "repo_id", "account_id", "actor", "source"},
        "published_session_branch": {"session_id", "repo_id", "git_branch"},
        "published_span": {"span_id", "session_id", "repo_id", "account_id",
                           "thread_role"},
        "published_withheld": {"account_id", "host_id"},
        # Migration 004: what each publisher ASSERTED about the repo, kept
        # per publisher so that a caller who reaches a repo only by having
        # published into it is shown what they sent rather than what a
        # stranger stored -- otherwise the difference answers "does anybody
        # here work on acme/skunkworks". Every one of these is a copy of a
        # column already allowed on `published_repo`: a remote URL and its
        # parsed parts, public on the far side of the boundary by
        # docs/REDACTION.md §1, and not one of them a path or free prose.
        "repo_publisher": {"repo_id", "account_id", "remote_url", "forge",
                           "owner", "repo", "web_url", "name"},
        "team_repo": {"team_id", "repo_id", "added_by"},
    }
    with migrated_db.connection() as conn:
        live = team_tables(conn)
        assert set(allowed) <= live, (
            f"`allowed` describes table(s) that are not in the team store any "
            f"more: {sorted(set(allowed) - live)}"
        )
        for table in sorted(live):
            found = _free_text_columns(conn, table)
            expected = allowed.get(table)
            assert expected is not None, (
                f"{table} is in the team store and this test has never heard of "
                f"it. Its free-text columns are {found}. Add it to `allowed` "
                f"having checked every one of them, or give it a reason in "
                f"CONTROL_PLANE -- a table nobody listed used to be a table "
                f"nobody checked."
            )
            assert set(found) == expected, (
                f"{table} free-text columns drifted: {found} (expected {sorted(expected)})"
            )


def test_every_personal_table_is_keyed_on_account(migrated_db):
    """Without account_id in the primary key, two accounts share a row.

    `project_id = sha256(root_path)`, so two people at `/home/ci/work` compute
    the same id. With a single-column key those are one row: whoever pushes
    last overwrites the other, and the UNIQUE on `root_path` turns a
    coincidence into an error that tells account A something true about
    account B's disk. See the header of migration 002.
    """
    with migrated_db.connection() as conn:
        for table in personal_schema.BY_NAME:
            key = [
                r["column_name"]
                for r in conn.execute(
                    """SELECT a.attname AS column_name
                       FROM pg_index i
                       JOIN pg_attribute a
                         ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                       WHERE i.indrelid = %s::regclass AND i.indisprimary""",
                    (table,),
                )
            ]
            assert "account_id" in key, f"{table} primary key {key} is not account-scoped"


def test_personal_foreign_keys_carry_account_id(migrated_db):
    """A composite FK makes a cross-account reference unrepresentable.

    The application filters on account_id everywhere, but "everywhere" is a
    claim about code that keeps being written. This is the same claim made by
    the database, where it cannot be forgotten in a new handler.
    """
    with migrated_db.connection() as conn:
        rows = conn.execute(
            """SELECT c.conname, c.conrelid::regclass::text AS child,
                      c.confrelid::regclass::text AS parent,
                      array_agg(a.attname ORDER BY a.attnum) AS cols
               FROM pg_constraint c
               JOIN unnest(c.conkey) AS k(attnum) ON true
               JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
               WHERE c.contype = 'f'
                 AND c.conrelid::regclass::text = ANY(%s::text[])
                 AND c.confrelid::regclass::text = ANY(%s::text[])
               GROUP BY c.conname, c.conrelid, c.confrelid""",
            (list(personal_schema.BY_NAME), list(personal_schema.BY_NAME)),
        ).fetchall()
        assert rows, "no intra-personal-store foreign keys found at all"
        for r in rows:
            assert "account_id" in r["cols"], (
                f"{r['child']} -> {r['parent']} ({r['conname']}) does not carry "
                f"account_id: {r['cols']}"
            )


def test_thread_parent_reference_is_deferred(migrated_db):
    """A batch of threads contains children whose parents are in the same batch.

    Subagents nest several levels deep. Without the deferral, whether a push
    succeeds depends on the order rows happen to arrive in -- an intermittent
    failure that only reproduces on somebody else's machine.
    """
    with migrated_db.connection() as conn:
        row = conn.execute(
            """SELECT condeferrable, condeferred FROM pg_constraint
               WHERE contype = 'f' AND conrelid = 'thread'::regclass
                 AND confrelid = 'thread'::regclass"""
        ).fetchone()
        assert row is not None, "thread has no self-reference at all"
        assert row["condeferrable"] and row["condeferred"]


def test_thread_role_is_checked_by_the_database(migrated_db):
    """The three buckets partition active time; a fourth value breaks every total.

    Rejected in the route as well, but the CHECK is what holds when a row
    arrives by some other path -- a migration, a backfill, a psql session.
    """
    with migrated_db.connection() as conn:
        found = conn.execute(
            """SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
               WHERE contype = 'c' AND conrelid = 'published_span'::regclass"""
        ).fetchall()
        assert any("thread_role" in r["def"] for r in found)


def test_repo_id_matches_the_client(client_source):
    """The laptop and the server must compute the same repo_id or nothing joins.

    The client derives it from a git remote, the server from the GitHub API.
    A session published by the first is only visible through the second if
    both land on the same string, and `repoid.py` is a deliberate copy of four
    lines that has to be checked rather than trusted.
    """
    from cc_insights import grouping, redact

    for remote in (
        "git@github.com:lunowe/harbor-cli.git",
        "https://github.com/lunowe/harbor-cli",
        "ssh://git@github.com/lunowe/harbor-cli.git",
    ):
        normalized = grouping.normalize_remote(remote)
        assert repo_id(normalized) == redact.repo_id(normalized)

    assert github_remote("github.com", "lunowe/harbor-cli") == grouping.normalize_remote(
        "git@github.com:lunowe/harbor-cli.git"
    )


def test_mirrors_sync_tables(client_source):
    """`personal_schema` is a copy of `sync.TABLES`, and copies drift.

    `sync.py`'s own first test compared the module to a hardcoded copy of
    itself and sailed through a merge that added three tables. This reads the
    real thing. A failure means the client changed what it pushes and the
    server will reject it, or worse, silently accept a different shape.
    """
    from cc_insights import sync

    theirs = {t.name: t for t in sync.TABLES}
    mine = personal_schema.BY_NAME

    assert list(theirs) == list(mine), "table set or push order differs"
    for name, t in theirs.items():
        m = mine[name]
        owned = personal_schema.server_owned(name)
        expected = tuple(c for c in t.columns if c not in owned)
        assert expected == tuple(m.columns), f"{name}: columns differ"
        assert tuple(t.key) == tuple(m.key), f"{name}: key differs"
        assert (t.owner_filter is not None) == m.host_scoped, f"{name}: ownership differs"
        _assert_same_conflict_clause(name, t.set_clause, m.set_clause, owned)
        assert (t.parent or None) == (m.parent or None), f"{name}: self-reference differs"
    assert sync.EXCLUDED == personal_schema.EXCLUDED


def _assert_same_conflict_clause(name, theirs, mine, owned):
    """The server's clause is the client's, minus the server-owned assignments.

    `host.account_id` is the only case: the client updates it from the pushed
    row, and here it is the owner of the row and comes from the token. Checked
    as "a prefix plus assignments to declared server-owned columns" rather
    than skipped, so that any OTHER difference in the clause still fails --
    the conflict clauses are where `first_seen` stops moving forward and a
    pin stops being undone, and a silent divergence there is data loss.
    """
    theirs, mine = theirs or "", mine or ""
    if theirs == mine:
        return
    assert owned, f"{name}: conflict clause differs and nothing is server-owned"
    assert theirs.startswith(mine), f"{name}: conflict clause differs\n{theirs}\n{mine}"
    # Assignment targets only: a bare `column =` at the start of a
    # comma-separated chunk. `coalesce(excluded.account_id, host.account_id)`
    # contains a comma and must not read as a second assignment.
    extra = {m for m in re.findall(r"(?:^|,\s*)([a-z_]+)\s*=", theirs[len(mine):])}
    assert extra, f"{name}: conflict clause differs but assigns nothing new"
    assert extra <= set(owned), f"{name}: {sorted(extra - set(owned))} is not server-owned"


def test_optional_columns_are_exactly_the_clients_drift(client_source):
    """An `optional` column means the client's schema has it and sync does not.

    There are none today. `event.cache_write_1h_tokens` was the one, and
    commit 4485f28 added it to `sync.TABLES`, so it moved to `columns`. If
    this fails, somebody invented an optional column to avoid a conversation
    -- or sync caught up with another one and it needs the same move.
    """
    from cc_insights import redact, sync

    synced = {(t.name, c) for t in sync.TABLES for c in t.columns}
    classified = {(f.table, f.column) for f in redact.FIELDS}
    for t in personal_schema.TABLES:
        for col in t.optional:
            assert (t.name, col) not in synced, f"{t.name}.{col} IS in sync.TABLES now"
            assert (t.name, col) in classified, (
                f"{t.name}.{col} is not classified in redact.FIELDS"
            )


def test_omittable_columns_are_real_columns_the_client_may_not_send_yet(client_source):
    """`omittable` is the other direction of drift, and it must stay honest.

    An omittable column is part of the transfer -- `sync.TABLES` lists it --
    but an older installed client does not send it, so a push that leaves it
    out is accepted and the stored value is left alone. That leniency is
    exactly what would hide a genuine mistake: a column dropped from
    `sync.TABLES`, or a required column quietly demoted so a failing push
    would go green. So an omittable column must still BE in `sync.TABLES`,
    and it must never be missing from `columns`.
    """
    from cc_insights import sync

    synced = {(t.name, c) for t in sync.TABLES for c in t.columns}
    for t in personal_schema.TABLES:
        for col in t.omittable:
            assert col in t.columns, f"{t.name}.{col} is omittable but not a column"
            assert (t.name, col) in synced, (
                f"{t.name}.{col} is omittable but no longer in sync.TABLES; "
                "it is either `optional` again or it was removed"
            )
        # Nothing may be both: the two categories say opposite things about
        # whether `sync.TABLES` lists the column.
        assert not (set(t.optional) & set(t.omittable)), t.name


@pytest.fixture
def client_source():
    """Put the client package on the path, or skip.

    Skipping rather than failing, and saying which: these tests check that two
    packages agree, and they cannot check anything when only one is present.
    Reporting them as passed would be the dishonest option.
    """
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    if not (src / "cc_insights" / "sync.py").exists():
        pytest.skip("the cc_insights source tree is not next to this server")
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return src
