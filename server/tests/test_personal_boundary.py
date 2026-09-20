"""Account A cannot read account B's personal rows. Under any endpoint.

This is the file that matters most in the suite. The personal store holds
`~/Coding/<client-name>`, hostnames and every `cwd` an agent ran in --
docs/ACCOUNTS.md §2 says single-account readable, no exceptions, and no
admin-can-see-everything mode, because an admin who can read it is a second
person.

Each test states what a colleague would be able to read if the assertion
stopped holding.
"""

from __future__ import annotations

import pytest

from conftest import host_row, project_row, session_row

SECRET_PATH = "/Users/bob/Coding/AcmeCorp-Unreleased"


@pytest.fixture
def bob_with_data(client, bob):
    """Bob pushes a host, a project with a revealing path, and a session."""
    assert client.post("/v1/personal/push", json=host_row("h-bob", "bobs-laptop"),
                       headers=bob.auth).status_code == 200
    assert client.post("/v1/personal/push",
                       json=project_row("h-bob", "p-bob", SECRET_PATH),
                       headers=bob.auth).status_code == 200
    assert client.post("/v1/personal/push",
                       json=session_row("h-bob", "s-bob", "p-bob"),
                       headers=bob.auth).status_code == 200
    return bob


def test_pull_never_returns_another_accounts_rows(client, alice, bob_with_data):
    """A leak here hands over a colleague's directory names and client names.

    Every table, because "every query filters on account_id" is a claim about
    code and this is the check on it.
    """
    for table in ("host", "project", "project_group", "project_probe",
                  "session", "thread", "event", "span"):
        r = client.get(f"/v1/personal/pull?table={table}", headers=alice.auth)
        assert r.status_code == 200, (table, r.text)
        assert r.json()["rows"] == [], f"{table} leaked rows to another account"
        assert SECRET_PATH not in r.text


def test_pull_returns_your_own_rows(client, bob_with_data):
    """The complement. A boundary test that passes because nothing works is no test."""
    r = client.get("/v1/personal/pull?table=project", headers=bob_with_data.auth)
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert SECRET_PATH in rows[0]


def test_status_counts_only_your_own_rows(client, alice, bob_with_data):
    """Row counts are a side channel: "3 sessions" says somebody has been working."""
    body = client.get("/v1/personal/status", headers=alice.auth).json()
    assert body["totalRows"] == 0
    assert body["hosts"] == []
    assert all(t["rows"] == 0 for t in body["tables"])

    mine = client.get("/v1/personal/status", headers=bob_with_data.auth).json()
    assert mine["totalRows"] == 3
    assert mine["hosts"][0]["hostname"] == "bobs-laptop"


def test_you_cannot_push_against_another_accounts_host(client, alice, bob_with_data):
    """Claiming somebody's host id would attach your sessions to their machine.

    404 rather than 403: a host id belonging to somebody else must look
    exactly like one that does not exist, or the endpoint becomes an oracle
    for "is this host id real".
    """
    r = client.post("/v1/personal/push", json=session_row("h-bob", "s-alice"),
                    headers=alice.auth)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_the_same_host_id_under_two_accounts_is_two_rows(client, alice, bob_with_data):
    """Signing in as somebody else on one machine must not merge the histories.

    `host_id` is baked into every session id, so it is not regenerated on a
    claim (docs/ACCOUNTS.md §4). The composite key is what keeps two accounts
    that share a machine from sharing its row -- and from one overwriting the
    other's `hostname`.
    """
    assert client.post("/v1/personal/push", json=host_row("h-bob", "alices-name-for-it"),
                       headers=alice.auth).status_code == 200

    bobs = client.get("/v1/personal/pull?table=host", headers=bob_with_data.auth).json()
    alices = client.get("/v1/personal/pull?table=host", headers=alice.auth).json()
    assert bobs["rows"][0][1] == "bobs-laptop"
    assert alices["rows"][0][1] == "alices-name-for-it"


def test_the_same_project_id_under_two_accounts_does_not_collide(
    client, alice, bob, migrated_db
):
    """`project_id = sha256(root_path)`, so two CI boxes at /home/ci/work are one id.

    With a single-column primary key those would be ONE row: the second push
    overwrites the first, and the UNIQUE index on `root_path` turns a
    coincidence into an error that tells account A something true about
    account B's disk. This is the concrete failure the composite key in
    migration 002 prevents.
    """
    shared_path = "/home/ci/work"
    shared_id = "same-hash-both-sides"
    for who, host in ((alice, "h-a"), (bob, "h-b")):
        assert client.post("/v1/personal/push", json=host_row(host),
                           headers=who.auth).status_code == 200
        r = client.post("/v1/personal/push",
                        json=project_row(host, shared_id, shared_path),
                        headers=who.auth)
        assert r.status_code == 200, r.text

    with migrated_db.connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM project WHERE project_id = %s", (shared_id,)
        ).fetchone()["n"]
    assert n == 2, "two accounts' identically-pathed projects collapsed into one row"


def test_a_pushed_session_cannot_reference_another_accounts_project(client, alice, bob):
    """A composite foreign key makes the cross-account join unrepresentable.

    Even if a handler forgot its `account_id` filter, the database refuses.
    Bob's project exists; Alice naming it must fail as "not pushed yet"
    rather than silently attaching her session to his path.
    """
    client.post("/v1/personal/push", json=host_row("h-bob"), headers=bob.auth)
    client.post("/v1/personal/push", json=project_row("h-bob", "p-bob", SECRET_PATH),
                headers=bob.auth)
    client.post("/v1/personal/push", json=host_row("h-alice"), headers=alice.auth)

    r = client.post("/v1/personal/push",
                    json=session_row("h-alice", "s-alice", "p-bob"), headers=alice.auth)
    assert r.status_code == 409
    assert r.json()["error"] == "foreign_key_violation"


def test_there_is_no_parameter_that_widens_a_pull(client, alice, bob_with_data):
    """No `?accountId=`, no `?all=`, no admin mode. Extra parameters are inert.

    docs/ACCOUNTS.md §2 rules out an admin who can read this store. If one of
    these ever started working, it would be the whole store, for every
    account, through one query string.
    """
    for extra in ("&accountId=" + bob_with_data.account_id, "&all=1",
                  "&account=" + bob_with_data.account_id, "&admin=true"):
        r = client.get(f"/v1/personal/pull?table=project{extra}", headers=alice.auth)
        assert r.status_code == 200
        assert r.json()["rows"] == []


def test_an_error_message_does_not_quote_another_accounts_row(client, alice, bob_with_data):
    """Driver errors can echo the offending row, and the row here is a path.

    A 409 that pasted a colleague's `root_path` into Alice's console would
    leak through the error channel what the data channel refuses.
    """
    r = client.post("/v1/personal/push",
                    json=session_row("h-bob", "s-x", "p-bob"), headers=alice.auth)
    assert SECRET_PATH not in r.text
