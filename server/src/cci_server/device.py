"""The device authorisation grant, as state in a table.

Separated from the routes because the interesting part is a state machine with
five exits, four of which GitHub will not produce on demand. Keeping it here
means the tests drive `start` and `poll` directly against a scripted provider
and a real database, rather than through HTTP with a mock in the middle.

TWO DEVICE CODES, and confusing them would be the leak.
`device_code` in this module's arguments is OURS -- the one the client polls
us with, stored hashed because for the life of the flow it can be exchanged
for a real token. GitHub's lives in `provider_device_code` and never leaves
the server. docs/ACCOUNTS.md §6 is the reason: a GitHub token is a credential
for every repo a person can reach, and the token we issue reads one account's
agent-time metadata. Handing the stronger one to a CLI that writes it to disk
would invert the trade the whole flow exists to make.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from cci_server import github, ids, tokens
from cci_server.db import now_ms

#: RFC 8628 §3.5: on `slow_down` the poller raises its interval by 5 seconds.
#: Applied to the stored interval rather than echoed once, so the back-off is
#: sticky. A client that reads the error code and ignores the number still
#: gets a bigger number on its next poll.
SLOW_DOWN_STEP_S = 5

PENDING = "pending"
COMPLETE = "complete"
DENIED = "denied"
EXPIRED = "expired"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class Outcome:
    """What `poll` decided. Exactly one of `error` or `token_response`."""

    status: int = 200
    error: str | None = None
    message: str = ""
    #: Set only for the two RETRYABLE codes. Its presence is how a client can
    #: tell "wait and try again" from "stop" without a lookup table.
    retry_after_s: int | None = None
    token_response: dict | None = None


def start(conn, provider: github.Provider, *, scope: str, client_name: str | None,
          ttl_s: int) -> dict:
    """Begin a flow. Returns the body of `POST /v1/auth/device/start`."""
    upstream = provider.start_device(scope)
    plain = "dev_" + secrets.token_urlsafe(24)
    now = now_ms()
    # Ours must not outlive GitHub's, or a client polls happily against a code
    # GitHub has already forgotten and learns it expired much later than it
    # actually did.
    expires_at = now + min(ttl_s, upstream.expires_in) * 1000
    conn.execute(
        """INSERT INTO device_authorization
               (device_id, device_code_hash, user_code, provider, provider_device_code,
                verification_uri, client_name, interval_s, created_at, expires_at, status)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (ids.new_id(ids.DEVICE), _hash(plain), upstream.user_code, github.GITHUB,
         upstream.device_code, upstream.verification_uri, client_name,
         upstream.interval, now, expires_at, PENDING),
    )
    return {
        "deviceCode": plain,
        "userCode": upstream.user_code,
        "verificationUri": upstream.verification_uri,
        "verificationUriComplete":
            f"{upstream.verification_uri}?user_code={upstream.user_code}",
        "expiresAt": expires_at,
        "interval": upstream.interval,
    }


def _bump(conn, device_id: str, current: int) -> int:
    raised = current + SLOW_DOWN_STEP_S
    conn.execute(
        "UPDATE device_authorization SET interval_s = %s WHERE device_id = %s",
        (raised, device_id),
    )
    return raised


def _finish(conn, device_id: str, status: str, account_id: str | None = None) -> None:
    conn.execute(
        """UPDATE device_authorization
           SET status = %s, completed_at = %s, account_id = %s WHERE device_id = %s""",
        (status, now_ms(), account_id, device_id),
    )


def poll(conn, provider: github.Provider, device_code: str, *, settings) -> Outcome:
    """One poll. Every branch is documented because every branch is a client bug waiting.

    Order matters here. The local rate limit is checked BEFORE the upstream
    call, because its whole purpose is to keep an impatient CLI from spending
    this instance's GitHub rate limit -- once that is gone, nobody can sign in,
    not just the client that burned it.
    """
    row = conn.execute(
        """SELECT device_id, provider_device_code, interval_s, last_polled_at,
                  expires_at, status
           FROM device_authorization WHERE device_code_hash = %s""",
        (_hash(device_code),),
    ).fetchone()

    if row is None:
        return Outcome(400, "invalid_device_code",
                       "No device authorization matches that code. Start a new one.")

    now = now_ms()

    # A completed flow is kept rather than deleted, so a replay -- from a shell
    # history, a log, a retried request -- gets this instead of minting a
    # second token for a code somebody else may now be holding.
    if row["status"] == COMPLETE:
        return Outcome(400, "invalid_device_code",
                       "That device code has already been exchanged.")
    if row["status"] == DENIED:
        return Outcome(400, github.DENIED, "The sign-in was declined.")
    if row["status"] == EXPIRED or now >= row["expires_at"]:
        if row["status"] != EXPIRED:
            _finish(conn, row["device_id"], EXPIRED)
        return Outcome(400, github.EXPIRED,
                       "The device authorization expired. Start a new one.")

    interval = int(row["interval_s"])
    last = row["last_polled_at"]
    if last is not None and now - last < interval * 1000:
        raised = _bump(conn, row["device_id"], interval)
        return Outcome(400, github.SLOW_DOWN,
                       f"Polling faster than the {interval}s interval. "
                       f"Wait {raised}s between polls.",
                       retry_after_s=raised)

    conn.execute(
        "UPDATE device_authorization SET last_polled_at = %s WHERE device_id = %s",
        (now, row["device_id"]),
    )

    try:
        result = provider.poll_token(row["provider_device_code"])
    except github.UpstreamError as exc:
        # Not the client's fault and usually transient, so it is a 502 and not
        # one of the flow's own error codes. A client must not treat it as
        # terminal, and must not treat it as "keep polling at full speed"
        # either -- hence the interval.
        return Outcome(502, "upstream_unavailable", f"GitHub could not be reached: {exc}",
                       retry_after_s=interval)

    if result.error == github.PENDING:
        return Outcome(400, github.PENDING, "Waiting for the browser approval.",
                       retry_after_s=interval)

    if result.error == github.SLOW_DOWN:
        raised = _bump(conn, row["device_id"], interval)
        return Outcome(400, github.SLOW_DOWN,
                       f"GitHub asked us to slow down. Wait {raised}s between polls.",
                       retry_after_s=raised)

    if result.error == github.EXPIRED:
        _finish(conn, row["device_id"], EXPIRED)
        return Outcome(400, github.EXPIRED,
                       "The device authorization expired. Start a new one.")

    if result.error == github.DENIED:
        _finish(conn, row["device_id"], DENIED)
        return Outcome(400, github.DENIED, "The sign-in was declined.")

    if result.error is not None or not result.access_token:
        # Anything GitHub sends that is not one of the four is a bug on one
        # side or the other. It must NOT be translated into "keep polling":
        # a client that retries forever on an unknown error never tells its
        # user what went wrong.
        return Outcome(502, "upstream_unavailable",
                       f"GitHub returned an unexpected error: {result.error!r}")

    return _complete(conn, provider, row, result)


def _complete(conn, provider: github.Provider, row, result: github.TokenResult) -> Outcome:
    """Exchange a granted GitHub token for one of ours, then forget theirs.

    The GitHub token exists inside this function and nowhere else -- not in a
    column, not in the response. Everything derived from it is derived now:
    the stable subject id, the display login, and the repository access list
    of docs/SERVER_API.md §4.6.
    """
    identity = provider.identify(result.access_token)
    account_id, actor = _account_for(conn, github.GITHUB, identity)
    _refresh_repo_access(conn, provider, account_id, result.access_token)

    plain = tokens.issue(conn, account_id, _token_name(conn, row["device_id"]))
    _finish(conn, row["device_id"], COMPLETE, account_id)

    return Outcome(
        200,
        token_response={
            "accessToken": plain,
            "tokenType": "bearer",
            "accountId": account_id,
            # Shown so a person can see WHICH identity they just signed in as
            # before any data moves. Publishing a week of work under the wrong
            # account is not something an undo exists for.
            "actor": actor,
            # No clock expiry. A CLI token that expires silently mid-week turns
            # a working background job into a quiet capture gap, which is the
            # exact failure `cci doctor` exists because of. Revocation is the
            # control.
            "expiresAt": None,
        },
    )


def _token_name(conn, device_id: str) -> str | None:
    row = conn.execute(
        "SELECT client_name FROM device_authorization WHERE device_id = %s", (device_id,)
    ).fetchone()
    return row["client_name"] if row else None


def _account_for(conn, provider_name: str, identity: github.ForgeIdentity) -> tuple[str, str]:
    """The account behind a forge identity, created on first sign-in.

    Keyed on `(provider, subject)` where subject is GitHub's NUMERIC id. Not
    the login: logins are renameable and reusable, so keying on one hands an
    account to whoever claims an abandoned name next.

    `actor` is set once, at creation, and is NOT updated when the login
    changes. Published rows carry the actor they were stamped with, and
    rewriting the account's would make old rows and new rows disagree about
    who did the work; `account_id` is the stable join and `identity.label`
    carries the current name for display.
    """
    now = now_ms()
    found = conn.execute(
        """SELECT i.account_id, a.actor FROM identity i
           JOIN account a ON a.account_id = i.account_id
           WHERE i.provider = %s AND i.subject = %s""",
        (provider_name, identity.subject),
    ).fetchone()
    if found:
        conn.execute(
            "UPDATE identity SET label = %s, updated_at = %s "
            "WHERE provider = %s AND subject = %s",
            (identity.login, now, provider_name, identity.subject),
        )
        return found["account_id"], found["actor"]

    account_id = ids.new_id(ids.ACCOUNT)
    actor = _unique_actor(conn, identity.login)
    with conn.transaction():
        conn.execute(
            "INSERT INTO account (account_id, actor, created_at) VALUES (%s, %s, %s)",
            (account_id, actor, now),
        )
        conn.execute(
            """INSERT INTO identity
                   (identity_id, account_id, provider, subject, label, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (ids.new_id(ids.IDENTITY), account_id, provider_name, identity.subject,
             identity.login, now, now),
        )
    return account_id, actor


def _unique_actor(conn, login: str) -> str:
    """`login`, or `login~2` if somebody already has it.

    GitHub logins are unique, so this only fires across providers -- the email
    sign-in docs/ACCOUNTS.md §1 plans next, where a person could pick a local
    name that collides. Suffixing rather than failing, because the alternative
    is a sign-in that cannot succeed and that the person cannot fix.
    """
    candidate = login
    n = 1
    while conn.execute(
        "SELECT 1 FROM account WHERE actor = %s", (candidate,)
    ).fetchone():
        n += 1
        candidate = f"{login}~{n}"
    return candidate


def _refresh_repo_access(conn, provider: github.Provider, account_id: str,
                         access_token: str) -> None:
    """Record which repos this identity can reach, then drop the token.

    Best-effort on purpose. A token whose scopes do not cover repositories, or
    a GitHub having a bad minute, must not stop somebody signing in: team
    rosters are the primary mechanism and this is additive
    (docs/SERVER_API.md §4.6).

    An EMPTY result does not clear what is already stored. An instance
    configured with `read:user` only would otherwise wipe a previously
    verified list on every sign-in, silently narrowing somebody's scope to
    nothing -- and "the dashboard went empty after I logged in again" is a
    bug nobody would connect to an OAuth scope.
    """
    try:
        repo_ids = provider.accessible_repo_ids(access_token)
    except github.UpstreamError:
        return
    if not repo_ids:
        return
    now = now_ms()
    with conn.transaction():
        conn.execute(
            "DELETE FROM account_repo_access WHERE account_id = %s AND provider = %s",
            (account_id, github.GITHUB),
        )
        conn.cursor().executemany(
            """INSERT INTO account_repo_access (account_id, repo_id, provider, verified_at)
               VALUES (%s, %s, %s, %s) ON CONFLICT (account_id, repo_id) DO NOTHING""",
            [(account_id, r, github.GITHUB, now) for r in repo_ids],
        )
