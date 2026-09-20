"""Test fixtures: a real PostgreSQL, a scripted GitHub, and two accounts.

AGAINST A REAL DATABASE, not a fake one. Half of what this server enforces is
enforced by the schema -- composite keys that make a cross-account join
unrepresentable, a CHECK on `thread_role`, a deferred self-reference on
`thread.parent_thread_id`, and the absence of any path column in the team
store. A stub would pass every one of those tests while proving nothing about
the thing that actually runs.

When no PostgreSQL is reachable the whole suite SKIPS rather than falling back
to something weaker, and says so. A green run against a substitute would be a
worse outcome than a skipped one, because only one of the two is honest about
what was checked.

Set `CCI_SERVER_TEST_ADMIN_URL` to point at a different server. The default is
a local socket, which is what a developer on this project already has.
"""

from __future__ import annotations

import os
import secrets

import pytest

psycopg = pytest.importorskip("psycopg", reason="psycopg is required to test this server")

from cci_server import config, github, ids, tokens  # noqa: E402
from cci_server.app import create_app  # noqa: E402
from cci_server.db import Database, now_ms  # noqa: E402

ADMIN_URL = os.environ.get("CCI_SERVER_TEST_ADMIN_URL", "postgresql:///postgres")

#: Every table, child before parent, for the truncate between tests.
_TABLES = [
    "published_span", "published_session_branch", "published_session",
    "repo_publisher", "team_repo", "published_repo", "published_withheld",
    "span", "event", "thread", "session", "project_probe", "project",
    "project_group", "host",
    "account_repo_access", "device_authorization", "api_token",
    "team_invite_redemption", "team_member", "team_invite", "team",
    "identity", "account",
]


def _reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    if not _reachable(ADMIN_URL):
        pytest.skip(
            f"no PostgreSQL at {ADMIN_URL!r}; set CCI_SERVER_TEST_ADMIN_URL. "
            "These tests are not run against a substitute on purpose -- see "
            "the docstring in conftest.py."
        )
    name = f"cci_server_test_{secrets.token_hex(6)}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    url = _swap_dbname(ADMIN_URL, name)
    try:
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _swap_dbname(url: str, name: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{name}"


@pytest.fixture(scope="session")
def migrated_db(database_url):
    db = Database(database_url, max_size=4)
    db.migrate()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(migrated_db):
    """Empty every table before each test.

    TRUNCATE rather than recreating the database: the schema is the expensive
    part and it is also the part under test, so rebuilding it 40 times would
    mostly measure PostgreSQL's DDL speed.
    """
    with migrated_db.connection() as conn:
        conn.execute("TRUNCATE " + ", ".join(_TABLES) + " CASCADE")
    yield


@pytest.fixture
def provider() -> github.FakeProvider:
    return github.FakeProvider()


@pytest.fixture
def settings(database_url) -> config.Settings:
    return config.from_env(
        {
            "CCI_SERVER_DATABASE_URL": database_url,
            "CCI_SERVER_GITHUB_CLIENT_ID": "test-client",
            "CCI_SERVER_GITHUB_CLIENT_SECRET": "test-secret",
            "CCI_SERVER_GITHUB_SCOPE": "read:user repo",
        }
    )


@pytest.fixture
def app(settings, migrated_db, provider):
    return create_app(settings, db=migrated_db, provider=provider, migrate=False)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


class Account:
    """An account plus a live token, for tests that are not about sign-in."""

    def __init__(self, account_id: str, actor: str, token: str) -> None:
        self.account_id = account_id
        self.actor = actor
        self.token = token

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def make_account(migrated_db):
    """Create an account directly, bypassing the device flow.

    Sign-in is tested on its own. Every other test needs two accounts to exist
    and does not need them to have been created the slow way.
    """

    def _make(actor: str) -> Account:
        account_id = ids.new_id(ids.ACCOUNT)
        now = now_ms()
        with migrated_db.connection() as conn:
            conn.execute(
                "INSERT INTO account (account_id, actor, created_at) VALUES (%s, %s, %s)",
                (account_id, actor, now),
            )
            conn.execute(
                """INSERT INTO identity
                       (identity_id, account_id, provider, subject, label,
                        created_at, updated_at)
                   VALUES (%s, %s, 'github', %s, %s, %s, %s)""",
                (ids.new_id(ids.IDENTITY), account_id, f"gh-{actor}", actor, now, now),
            )
            token = tokens.issue(conn, account_id, f"test token for {actor}")
        return Account(account_id, actor, token)

    return _make


def join_team(client, admin: Account, team_id: str, joiner: Account,
              role: str = "member") -> dict:
    """Put `joiner` on `team_id` the only way there is: a code they redeem.

    Every test that used to call `POST /v1/teams/{id}/members` with an
    `accountId` now comes through here, and that is deliberately not a
    like-for-like swap. It routes the setup of every membership test through
    the real consent path, so an attack written against a team's membership
    is attacking the arrangement that actually ships -- the old helper would
    have kept those tests green against a join flow that had been broken or
    removed.

    Two calls, because two people are involved and that is the whole point:
    the admin mints with their own token, the joiner redeems with theirs.
    """
    minted = client.post(f"/v1/teams/{team_id}/invites", json={"role": role},
                         headers=admin.auth)
    assert minted.status_code == 201, minted.text
    code = minted.json()["code"]
    joined = client.post("/v1/teams/join", json={"code": code}, headers=joiner.auth)
    assert joined.status_code == 200, joined.text
    return joined.json()


@pytest.fixture
def alice(make_account) -> Account:
    return make_account("alice")


@pytest.fixture
def bob(make_account) -> Account:
    return make_account("bob")


# --------------------------------------------------------------------------
# row builders -- shaped exactly like what the client sends
# --------------------------------------------------------------------------


def host_row(host_id: str, hostname: str = "studio") -> dict:
    return {
        "hostId": host_id,
        "table": "host",
        "columns": ["host_id", "hostname", "os", "first_seen", "last_seen"],
        "rows": [[host_id, hostname, "darwin", 1_700_000_000_000, 1_700_000_100_000]],
    }


def project_row(host_id: str, project_id: str, root_path: str) -> dict:
    return {
        "hostId": host_id,
        "table": "project",
        "columns": ["project_id", "root_path", "name", "group_id", "group_pinned"],
        "rows": [[project_id, root_path, root_path.rsplit("/", 1)[-1], None, 0]],
    }


def session_row(host_id: str, session_id: str, project_id: str | None = None,
                started: int = 1_700_000_000_000) -> dict:
    return {
        "hostId": host_id,
        "table": "session",
        "columns": ["id", "native_id", "source", "host_id", "project_id", "cwd",
                    "git_branch", "cli_version", "started_at", "ended_at",
                    "event_count", "active_ms"],
        "rows": [[session_id, f"native-{session_id}", "claude_code", host_id,
                  project_id, "/Users/secret/Coding/thing", "main", "1.0",
                  started, started + 60_000, 10, 60_000]],
    }
