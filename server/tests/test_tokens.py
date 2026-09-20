"""Tokens: hashed at rest, revocable, and never another account's.

A token here reads one person's filesystem paths. Everything in this file is
about the difference between "the API refuses" and "the data is not there".
"""

from __future__ import annotations

from cci_server import tokens


def test_the_plaintext_token_is_never_stored(migrated_db, alice):
    """A database dump must not contain anything that can be replayed.

    If this fails, a backup, a read replica or one `psql` session hands over
    working credentials for every account on the instance.
    """
    with migrated_db.connection() as conn:
        rows = conn.execute("SELECT token_hash FROM api_token").fetchall()
    stored = {r["token_hash"] for r in rows}
    assert alice.token not in stored
    assert tokens.hash_token(alice.token) in stored
    assert all(len(h) == 64 for h in stored)


def test_no_token_is_401(client):
    """A missing token must say "sign in", not "not found"."""
    r = client.get("/v1/personal/status")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthenticated"


def test_a_garbage_token_is_401_and_says_nothing_else(client):
    """Unknown, revoked and expired are one answer on purpose.

    Telling them apart tells somebody holding a stolen token whether it was
    ever valid, which is the one thing they cannot otherwise learn.
    """
    for header in ("Bearer ccis_nope", "Basic abc", "ccis_nope", "Bearer "):
        r = client.get("/v1/personal/status", headers={"Authorization": header})
        assert r.status_code == 401, header
        assert r.json()["error"] == "unauthenticated"


def test_a_revoked_token_stops_working_immediately(client, alice):
    """Revocation is the only expiry this design has, so it has to be instant."""
    assert client.get("/v1/personal/status", headers=alice.auth).status_code == 200

    tok = client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"][0]
    assert client.delete(f"/v1/auth/tokens/{tok['tokenId']}",
                         headers=alice.auth).status_code == 204

    after = client.get("/v1/personal/status", headers=alice.auth)
    assert after.status_code == 401


def test_logout_revokes_the_presenting_token_without_knowing_its_id(client, alice):
    """`cci logout` must work on the machine somebody is signing out of.

    Making it look its own id up first would be one more round trip to fail
    on exactly the machine that is being decommissioned.
    """
    assert client.post("/v1/auth/logout", headers=alice.auth).status_code == 204
    assert client.get("/v1/auth/whoami", headers=alice.auth).status_code == 401


def test_one_account_cannot_revoke_anothers_token(client, alice, bob):
    """A 404 and not a 403: a distinguishable 403 confirms the id exists."""
    bob_token = client.get("/v1/auth/tokens", headers=bob.auth).json()["tokens"][0]
    r = client.delete(f"/v1/auth/tokens/{bob_token['tokenId']}", headers=alice.auth)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    # And Bob's token still works.
    assert client.get("/v1/auth/whoami", headers=bob.auth).status_code == 200


def test_listing_tokens_never_returns_the_token_or_its_hash(client, alice):
    """Publishing the hash makes an offline check against a stolen dump possible."""
    body = client.get("/v1/auth/tokens", headers=alice.auth).json()
    text = str(body)
    assert alice.token not in text
    from cci_server.tokens import hash_token

    assert hash_token(alice.token) not in text
    assert body["tokens"][0]["tokenId"].startswith("tok_")


def test_a_revoked_token_stays_listed_so_the_revocation_is_visible(
    client, alice, migrated_db
):
    """Somebody checking a lost laptop needs to see that the token is dead.

    Seeing nothing is indistinguishable from never having clicked the button,
    and the person is standing there deciding whether to call somebody.
    """
    with migrated_db.connection() as conn:
        laptop = tokens.issue(conn, alice.account_id, "cci on the lost laptop")
    lost = next(
        t for t in client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"]
        if t["name"] == "cci on the lost laptop"
    )
    assert client.delete(f"/v1/auth/tokens/{lost['tokenId']}",
                         headers=alice.auth).status_code == 204

    listed = client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"]
    revoked = next(t for t in listed if t["tokenId"] == lost["tokenId"])
    assert revoked["revokedAt"] is not None
    # And it really is dead, not merely labelled.
    assert client.get("/v1/auth/whoami",
                      headers={"Authorization": f"Bearer {laptop}"}).status_code == 401


def test_revoking_twice_is_not_an_error_and_keeps_the_first_timestamp(
    client, alice, migrated_db
):
    """The caller's goal is already true, and the FIRST revocation is the fact.

    Moving the timestamp on a retry would misreport when access actually
    ended, which is the one thing anybody reads this column for.
    """
    with migrated_db.connection() as conn:
        tokens.issue(conn, alice.account_id, "spare")
    spare = next(
        t for t in client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"]
        if t["name"] == "spare"
    )
    client.delete(f"/v1/auth/tokens/{spare['tokenId']}", headers=alice.auth)
    first = next(
        t for t in client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"]
        if t["tokenId"] == spare["tokenId"]
    )["revokedAt"]

    assert client.delete(f"/v1/auth/tokens/{spare['tokenId']}",
                         headers=alice.auth).status_code == 204
    again = next(
        t for t in client.get("/v1/auth/tokens", headers=alice.auth).json()["tokens"]
        if t["tokenId"] == spare["tokenId"]
    )["revokedAt"]
    assert again == first


def test_whoami_reports_identities_as_a_list_today(client, alice):
    """`identity` is a table so email login needs no migration or client change.

    A client that renders a single provider now will need changing later; one
    that renders the list will not. docs/ACCOUNTS.md §4.
    """
    body = client.get("/v1/auth/whoami", headers=alice.auth).json()
    assert isinstance(body["identities"], list)
    assert body["identities"][0]["provider"] == "github"
    assert body["actor"] == "alice"
    assert body["token"]["tokenId"].startswith("tok_")
