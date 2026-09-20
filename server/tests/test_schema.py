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

#: Tables that hold the redacted projection. Anything a colleague can read.
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


def _columns(conn, table: str) -> list[str]:
    return [
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
    ]


def test_team_store_has_no_path_column(migrated_db):
    """A path column in the team store is a leak waiting for one careless INSERT.

    docs/REDACTION.md §2: the shared database cannot leak what it never
    received. The enforcement is that there is nowhere to put a path -- not a
    nullable column nobody writes to, which is one bug away from being
    written to and which no code review reliably catches. If this fails,
    somebody added a column that can hold `~/Coding/<client-name>`, and the
    projection stopped being a projection.
    """
    with migrated_db.connection() as conn:
        for table in TEAM_TABLES:
            cols = _columns(conn, table)
            assert cols, f"{table} does not exist"
            offending = sorted(set(cols) & set(FORBIDDEN_IN_TEAM_STORE))
            assert not offending, f"{table} has path-derived column(s) {offending}"


def test_team_store_holds_no_free_text_beyond_the_branch_name(migrated_db):
    """Metadata only. A free-text column is where prompt text ends up.

    The team store's TEXT columns are ids, a remote URL, its parsed parts, an
    actor, a source, a thread role and the branch name. If this list grows,
    somebody added somewhere for prose to live, and "no prompt text in the
    schema" is a release blocker in this project rather than a style rule.
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
        for table, expected in allowed.items():
            text_cols = {
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s "
                    "AND data_type IN ('text', 'character varying')",
                    (table,),
                )
            }
            assert text_cols == expected, f"{table} text columns drifted: {text_cols}"


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
