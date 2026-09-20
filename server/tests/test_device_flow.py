"""Sign-in: the four error branches, and the two things that must not leak.

GitHub will not produce `slow_down` or `expired_token` on request, so these
run against `github.FakeProvider`. That is not a way of avoiding the test --
it is the only way the branches a CLI gets wrong can be tested at all.
"""

from __future__ import annotations

from cci_server import github
from cci_server.github import ForgeIdentity, TokenResult


def _grant(provider: github.FakeProvider, device_code: str, token: str = "gho_real",
           login: str = "lunowe", subject: str = "1234567") -> None:
    provider.script[device_code] = [TokenResult(access_token=token)]
    provider.identities[token] = ForgeIdentity(subject=subject, login=login)


def test_start_returns_a_code_for_the_person_and_one_for_the_client(client, provider):
    """The person types `userCode`; the client polls with `deviceCode`.

    If these were one value, the string a person reads aloud over a call
    would be the string that exchanges for a bearer token.
    """
    r = client.post("/v1/auth/device/start", json={"clientName": "cci on studio"})
    assert r.status_code == 200
    body = r.json()
    assert body["deviceCode"].startswith("dev_")
    assert body["userCode"] and body["userCode"] != body["deviceCode"]
    assert body["interval"] >= 1
    assert body["expiresAt"] > 0
    assert body["userCode"] in body["verificationUriComplete"]


def test_our_device_code_is_not_githubs(client, provider, migrated_db):
    """GitHub's device code must never reach the client.

    It is a credential in GitHub's flow. The whole point of proxying the grant
    (docs/ACCOUNTS.md §6) is that the client holds nothing GitHub would
    accept.
    """
    body = client.post("/v1/auth/device/start", json={}).json()
    with migrated_db.connection() as conn:
        row = conn.execute(
            "SELECT provider_device_code, device_code_hash FROM device_authorization"
        ).fetchone()
    assert row["provider_device_code"] != body["deviceCode"]
    assert row["provider_device_code"] not in str(body)
    # And ours is stored hashed: a database dump must not yield a code that
    # can still be exchanged for a token.
    assert row["device_code_hash"] != body["deviceCode"]


def test_authorization_pending_tells_the_client_to_wait(client, provider):
    """The common case. A client that treats this as fatal never signs anyone in."""
    body = client.post("/v1/auth/device/start", json={}).json()
    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.status_code == 400
    assert r.json()["error"] == "authorization_pending"
    assert r.json()["interval"] == body["interval"]
    assert r.headers["Retry-After"] == str(body["interval"])


def test_polling_faster_than_the_interval_is_slowed_down_without_asking_github(
    client, provider
):
    """One impatient CLI must not spend the whole instance's GitHub rate limit.

    If this fails, a single client in a tight loop gets the server
    rate-limited at GitHub and nobody -- not just that client -- can sign in.
    """
    body = client.post("/v1/auth/device/start", json={}).json()
    code = body["deviceCode"]
    gh_code = "gh-device-1"
    polls_before = len(provider.script[gh_code])

    first = client.post("/v1/auth/device/token", json={"deviceCode": code})
    assert first.json()["error"] == "authorization_pending"

    second = client.post("/v1/auth/device/token", json={"deviceCode": code})
    assert second.status_code == 400
    assert second.json()["error"] == "slow_down"
    # The interval went UP and stayed up: a sticky back-off, not an echo.
    assert second.json()["interval"] > body["interval"]
    assert second.headers["Retry-After"] == str(second.json()["interval"])

    third = client.post("/v1/auth/device/token", json={"deviceCode": code})
    assert third.json()["error"] == "slow_down"
    assert third.json()["interval"] > second.json()["interval"]
    # The scripted provider was never consulted for the throttled polls.
    assert len(provider.script[gh_code]) == polls_before


def test_slow_down_from_github_raises_our_interval_too(client, provider, migrated_db):
    """GitHub's back-off has to reach the client, not stop at the server.

    The server is the poller GitHub is talking to, but the client is the one
    setting the pace. Absorbing the signal here would keep the client hammering
    at the old interval forever.
    """
    body = client.post("/v1/auth/device/start", json={}).json()
    provider.script["gh-device-1"] = [TokenResult(error=github.SLOW_DOWN)]
    _expire_throttle(migrated_db)

    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.status_code == 400
    assert r.json()["error"] == "slow_down"
    assert r.json()["interval"] == body["interval"] + 5


def test_expired_token_is_terminal_and_carries_no_retry_hint(client, provider):
    """A client must stop. The absence of `interval` is how it can tell."""
    body = client.post("/v1/auth/device/start", json={}).json()
    provider.script["gh-device-1"] = [TokenResult(error=github.EXPIRED)]

    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.status_code == 400
    assert r.json()["error"] == "expired_token"
    assert "interval" not in r.json()
    assert "Retry-After" not in r.headers


def test_access_denied_is_terminal(client, provider, migrated_db):
    """Somebody clicked cancel. Retrying would re-prompt them forever."""
    body = client.post("/v1/auth/device/start", json={}).json()
    provider.script["gh-device-1"] = [TokenResult(error=github.DENIED)]

    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.json()["error"] == "access_denied"

    # And it stays denied on a later poll, from our own record, without
    # asking GitHub again about a flow that is over.
    _expire_throttle(migrated_db)
    again = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert again.json()["error"] == "access_denied"


def test_an_unknown_github_error_is_not_translated_into_keep_polling(client, provider):
    """A client that retries forever on an unknown error never reports a failure."""
    body = client.post("/v1/auth/device/start", json={}).json()
    provider.script["gh-device-1"] = [TokenResult(error="unsupported_grant_type")]

    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.status_code == 502
    assert r.json()["error"] == "upstream_unavailable"


def test_a_successful_grant_mints_our_token_and_never_hands_over_githubs(
    client, provider, migrated_db
):
    """The token the client keeps must read agent-time metadata, not source code.

    A GitHub token is a credential for every repo the person can reach. If
    this assertion fails, `cci login` has written one to a file on disk.
    """
    body = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-1", token="gho_SECRET_GITHUB")

    r = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert r.status_code == 200
    out = r.json()
    assert out["accessToken"].startswith("ccis_")
    assert "gho_SECRET_GITHUB" not in r.text
    assert out["actor"] == "lunowe"
    assert out["expiresAt"] is None

    # Nor is GitHub's token anywhere in the database.
    with migrated_db.connection() as conn:
        for table, col in (("api_token", "token_hash"), ("identity", "label"),
                           ("device_authorization", "provider_device_code")):
            values = [r[col] for r in conn.execute(f"SELECT {col} FROM {table}")]
            assert "gho_SECRET_GITHUB" not in values

    whoami = client.get("/v1/auth/whoami", headers={"Authorization":
                                                    f"Bearer {out['accessToken']}"})
    assert whoami.status_code == 200
    assert whoami.json()["identities"][0]["subject"] == "1234567"


def test_a_device_code_cannot_be_exchanged_twice(client, provider, migrated_db):
    """A replay from a shell history must not mint a second token.

    The completed row is kept rather than deleted for exactly this. If it
    were deleted, a replay would look like an unknown code, which is the same
    answer -- but a second completion would look like a fresh sign-in.
    """
    body = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-1")
    first = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert first.status_code == 200

    _expire_throttle(migrated_db)
    second = client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    assert second.status_code == 400
    assert second.json()["error"] == "invalid_device_code"
    with migrated_db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM api_token").fetchone()["n"] == 1


def test_an_unknown_device_code_is_invalid_not_pending(client):
    """A made-up code must not look like a flow somebody could still approve."""
    r = client.post("/v1/auth/device/token", json={"deviceCode": "dev_nonsense"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_device_code"


def test_signing_in_twice_reuses_the_account_keyed_on_the_numeric_subject(
    client, provider, migrated_db
):
    """A renamed GitHub login must not create a second account.

    Logins are renameable and reusable; the numeric id is not. If this keyed
    on the login, the next person to claim an abandoned name would inherit
    somebody's corpus -- and the original owner would come back to an empty
    account.
    """
    first = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-1", token="t1", login="lunowe", subject="42")
    a = client.post("/v1/auth/device/token", json={"deviceCode": first["deviceCode"]}).json()

    second = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-2", token="t2", login="lunowe-renamed", subject="42")
    b = client.post("/v1/auth/device/token", json={"deviceCode": second["deviceCode"]}).json()

    assert a["accountId"] == b["accountId"]
    # `actor` is stamped once and does not follow a rename: published rows
    # already carry it, and rewriting it would make old and new rows disagree
    # about who did the work.
    assert b["actor"] == "lunowe"
    with migrated_db.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM account").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT label FROM identity"
        ).fetchone()["label"] == "lunowe-renamed"


def test_verified_repo_access_is_recorded_and_the_token_is_dropped(
    client, provider, migrated_db
):
    """docs/SERVER_API.md §4.6: derive scope from real access, keep no credential."""
    body = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-1", token="gho_x")
    provider.repos["gho_x"] = ["repo_aaa", "repo_bbb"]

    client.post("/v1/auth/device/token", json={"deviceCode": body["deviceCode"]})
    with migrated_db.connection() as conn:
        got = {r["repo_id"] for r in conn.execute(
            "SELECT repo_id FROM account_repo_access")}
    assert got == {"repo_aaa", "repo_bbb"}


def test_an_empty_access_list_does_not_wipe_what_was_verified_before(
    client, provider, migrated_db
):
    """An instance configured with `read:user` only must not narrow a scope to nothing.

    "The dashboard went empty after I logged in again" is a bug nobody would
    connect to an OAuth scope.
    """
    first = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-1", token="t1", subject="7")
    provider.repos["t1"] = ["repo_keep"]
    client.post("/v1/auth/device/token", json={"deviceCode": first["deviceCode"]})

    second = client.post("/v1/auth/device/start", json={}).json()
    _grant(provider, "gh-device-2", token="t2", subject="7")
    provider.repos["t2"] = []
    client.post("/v1/auth/device/token", json={"deviceCode": second["deviceCode"]})

    with migrated_db.connection() as conn:
        got = {r["repo_id"] for r in conn.execute(
            "SELECT repo_id FROM account_repo_access")}
    assert got == {"repo_keep"}


def _expire_throttle(db) -> None:
    """Pretend the client waited out its interval.

    The local rate limit is real and correct, and it would otherwise turn
    every multi-poll test into a test of the rate limit. Rewinding the clock
    on the stored poll time is the smallest lie that keeps the other branch
    under test.
    """
    with db.connection() as conn:
        conn.execute("UPDATE device_authorization SET last_polled_at = 0")
