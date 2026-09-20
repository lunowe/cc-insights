"""Bearer tokens: minting, hashing, looking up, revoking.

A token here is password-equivalent. It reads one account's `root_path`s,
`cwd`s and hostnames -- the data docs/REDACTION.md exists to keep away from a
second person. So the plaintext lives exactly twice: in the response that
minted it, and in a `0600` file in the client's config directory.
docs/ACCOUNTS.md §6 is specific that it does NOT go in `config.toml`, because
that file is plain text that people copy around.

WHY SHA-256 AND NOT BCRYPT.
This looks like the mistake everyone is warned about, so the reasoning is
written down. A password is low-entropy and human-chosen, which is why it
needs a deliberately slow hash: the attack is a dictionary, and the defence is
making each guess expensive. A token from `secrets.token_urlsafe(32)` is 256
bits of uniform randomness. There is no dictionary, and no strategy cheaper
than enumerating the keyspace, which a slow hash does not meaningfully change.

What a slow hash WOULD change is the cost of every request. Lookup is by hash,
so a per-row salt would mean a table scan and one KDF evaluation per stored
token on every call -- during a push of 191,475 rows, forty times over. Paying
that to defend against an attack the entropy already rules out would be the
expensive kind of wrong.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cci_server import ids
from cci_server.db import now_ms

#: Greppable on purpose. A secret scanner, a log filter and a person reading a
#: bug report can all recognise one, and a token pasted into the wrong field
#: fails as something other than "invalid".
TOKEN_PREFIX = "ccis_"


def mint() -> tuple[str, str]:
    """A new token and its hash. The plaintext is never stored."""
    plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plain, hash_token(plain)


def hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


@dataclass(frozen=True)
class Principal:
    """Who is making this request. Everything downstream derives from it."""

    account_id: str
    actor: str
    token_id: str


def parse_header(value: str | None) -> str | None:
    """The token out of an `Authorization` header, or None.

    Tolerant of case in the scheme, strict about everything else. A header that
    is nearly right is still wrong, and returning None here produces a 401 that
    tells the client to sign in -- which is the correct advice for every way
    this can fail.
    """
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def issue(conn, account_id: str, name: str | None, *, expires_at: int | None = None) -> str:
    """Store a new token for `account_id` and return the plaintext, once."""
    plain, digest = mint()
    conn.execute(
        """INSERT INTO api_token
               (token_id, account_id, token_hash, name, created_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (ids.new_id(ids.TOKEN), account_id, digest, name, now_ms(), expires_at),
    )
    return plain


def resolve(conn, plain: str) -> Principal | None:
    """The principal behind a token, or None if it may not be used.

    One query and one set of reasons to refuse -- unknown, revoked, expired --
    all collapsing to None, because the client's correct response to every one
    of them is the same: run `cci login` again. Distinguishing them in the
    response would tell an attacker holding a stolen token whether it was ever
    valid.
    """
    row = conn.execute(
        """SELECT t.token_id, t.account_id, t.revoked_at, t.expires_at, a.actor
           FROM api_token t JOIN account a ON a.account_id = t.account_id
           WHERE t.token_hash = %s""",
        (hash_token(plain),),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] <= now_ms():
        return None
    return Principal(
        account_id=row["account_id"], actor=row["actor"], token_id=row["token_id"]
    )


def touch(conn, token_id: str) -> None:
    """Record that a token was used.

    Separate from `resolve` and best-effort: this is the column a person reads
    to decide which of four laptops a token belongs to before revoking it, and
    it is not worth failing a request over. It is coarse on purpose -- a
    timestamp, not a count, not an IP -- because a server that logs where each
    of someone's machines is calling from has started collecting a second kind
    of data nobody asked it to hold.
    """
    conn.execute(
        "UPDATE api_token SET last_used_at = %s WHERE token_id = %s",
        (now_ms(), token_id),
    )


def revoke(conn, account_id: str, token_id: str) -> bool:
    """Revoke one of this account's tokens. True if it existed.

    Idempotent: revoking an already-revoked token succeeds and does not move
    `revoked_at`, because the caller's goal is already true and the first
    revocation is the one worth recording.
    """
    row = conn.execute(
        """UPDATE api_token SET revoked_at = COALESCE(revoked_at, %s)
           WHERE token_id = %s AND account_id = %s
           RETURNING token_id""",
        (now_ms(), token_id, account_id),
    ).fetchone()
    return row is not None


def listing(conn, account_id: str, *, retention_ms: int) -> list[dict]:
    """Live tokens, plus recently revoked ones so a revocation is visible."""
    cutoff = now_ms() - retention_ms
    rows = conn.execute(
        """SELECT token_id, name, created_at, last_used_at, expires_at, revoked_at
           FROM api_token
           WHERE account_id = %s AND (revoked_at IS NULL OR revoked_at >= %s)
           ORDER BY created_at DESC""",
        (account_id, cutoff),
    ).fetchall()
    return [
        {
            "tokenId": r["token_id"],
            "name": r["name"],
            "createdAt": r["created_at"],
            "lastUsedAt": r["last_used_at"],
            "expiresAt": r["expires_at"],
            "revokedAt": r["revoked_at"],
        }
        for r in rows
    ]
