"""Join codes: minting, hashing, redeeming, revoking. Consent, made a record.

`POST /v1/teams/{id}/members` used to take an `accountId` and add that person.
Nobody agreed, and the admin had no way to learn the id in the first place --
there is no directory endpoint and there must not be one, because a lookup
from a name to an `account_id` is an enumeration oracle over everybody on the
instance. So the route was unusable AND unsafe, which is a rare combination.

A join code replaces it. An admin mints one, sends it however they like, and
the colleague redeems it **with their own bearer token**. Three things fall
out of that shape and all three are the point:

  * The redemption is the consent. Nobody is on a roster who did not run
    `cci team join` themselves.
  * The account id never has to be discovered. The code says which team; the
    token says who is joining.
  * There is a record with two ends -- who minted, who redeemed, both stamped
    -- which is the audit trail for "how did they get in".

WHY THIS IS STORED LIKE A PASSWORD AND SIZED LIKE A KEY.
A join code does not authenticate anybody. What it does is grant, to whoever
holds it plus any account on this instance, the ability to read other people's
agent time. That makes it a bearer secret, and `tokens.py`'s discipline
applies without amendment: sha256 at rest, plaintext shown exactly once,
constant-time comparison, and the reasoning in that module's docstring for why
a slow KDF would be the expensive kind of wrong here too.

The sizing is where this differs from the other code in this system.
`device_authorization.user_code` is `43CA-9AAA` -- eight characters, and
correct for what it is: read off one screen, typed into another within
minutes, bound to one in-flight flow, throttled at GitHub's end. A join code
is the opposite on every count. It is pasted into Slack rather than typed, it
lives for days, and **this server has no attempt throttle at all**. There is
nothing to make guessing expensive except the size of the keyspace, so the
keyspace is the whole of `secrets.token_urlsafe(32)` -- 256 bits, comfortably
past the 128 the threat model asks for -- and nobody is expected to type it.

WHY EVERY FAILURE IS THE SAME FAILURE.
`redeem` returns `None` for unknown, revoked, expired, exhausted and
malformed, and the route turns all five into one 404 with one sentence. A
distinguishable "that code has expired" confirms that the code was real, which
confirms that the team was real, to somebody who just demonstrated they were
not invited to it. That is `errors.not_found`'s rule applied to a credential
rather than to a row, and it is the same reason `tokens.resolve` collapses
unknown, revoked and expired into one `None`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cci_server import ids, tokens
from cci_server.db import now_ms

#: Greppable, and deliberately NOT `ccis_`. A secret scanner should catch
#: both, and a person who pastes a join code into `cci login --token` should
#: get a failure that says something other than "invalid": the two credentials
#: authorise different things and confusing them is worth one clear error.
INVITE_PREFIX = "ccij_"

#: 32 bytes -> 43 url-safe base64 characters -> 256 bits. The threat model
#: asks for 128; this is the same width `tokens.mint` uses and there is no
#: reason for the weaker secret to be the one with a longer life.
CODE_BYTES = 32

#: Three days. Short, because the safe default for a secret that will sit in a
#: chat log forever is one that stops working before the week is out, and an
#: admin re-minting is one command. Long enough to survive a weekend and a
#: colleague who reads Slack on Monday, which is the case that would otherwise
#: push people towards `--expires-in 90d` out of irritation.
DEFAULT_TTL_MS = 72 * 60 * 60 * 1000

#: Thirty days, and the cap is the point rather than the number. `--expires-in`
#: exists so a real situation is not fought; an invite with no deadline at all
#: is the thing that must stay unreachable, because nobody revokes a code they
#: have forgotten they minted.
MAX_TTL_MS = 30 * 24 * 60 * 60 * 1000

#: Single-use by default. When the channel an admin pasted the code into turns
#: out to be wider than they thought, the blast radius is one person.
DEFAULT_MAX_USES = 1

#: A ceiling on `--uses`, so a typo is a bad day rather than an open door. An
#: onboarding code for a team of fifty is a real thing to want; an accidental
#: `--uses 10000` is not.
MAX_USES = 50


@dataclass(frozen=True)
class Minted:
    """A new code. `code` is the only time the plaintext exists on this side."""

    invite_id: str
    code: str
    team_id: str
    role: str
    created_at: int
    expires_at: int
    max_uses: int
    note: str | None


@dataclass(frozen=True)
class Joined:
    """The outcome of a redemption that worked."""

    team_id: str
    team_name: str
    role: str
    invite_id: str
    #: True when the account was already on the team. The redemption is then a
    #: no-op that consumes nothing -- see `redeem`.
    already_member: bool


def mint() -> tuple[str, str]:
    """A new join code and its hash. The plaintext is never stored."""
    plain = INVITE_PREFIX + secrets.token_urlsafe(CODE_BYTES)
    return plain, hash_code(plain)


def hash_code(plain: str) -> str:
    """sha256, the same function `tokens.py` uses, for the same reasons.

    Deliberately delegating rather than reimplementing: two hash functions for
    two kinds of bearer secret is two places for one of them to be changed to
    something reversible, and the day they disagreed nobody would notice
    because both would still round-trip against themselves.
    """
    return tokens.hash_token(plain)


def matches(stored_hash: str, presented: str) -> bool:
    """Constant-time comparison of a presented code against a stored hash.

    Lookup is BY hash and the column is UNIQUE, so PostgreSQL has already done
    an equality test by the time this is called and this is belt and braces.
    It is here anyway, for two reasons worth stating rather than assuming:

      * The index tells an attacker nothing useful, but the next person to
        write a code path here may well fetch a candidate row some other way
        -- by `invite_id` from a URL, say -- and compare it themselves. This
        is the function they will reach for, and it is already correct.
      * `==` on a hash digest leaks, through timing, how many leading
        characters of the digest matched. That is not a practical attack on a
        sha256 of a 256-bit secret, and it is also not a thing to have to
        reason about every time somebody reads this file.

    `test_a_join_code_is_compared_in_constant_time` fails if this becomes `==`.
    """
    return tokens.constant_time_eq(stored_hash, hash_code(presented))


def clamp_ttl(ttl_ms: int | None) -> int:
    """The requested lifetime, defaulted and capped. Never unbounded."""
    if ttl_ms is None:
        return DEFAULT_TTL_MS
    return max(1, min(int(ttl_ms), MAX_TTL_MS))


def clamp_uses(max_uses: int | None) -> int:
    return max(1, min(int(max_uses), MAX_USES)) if max_uses is not None else DEFAULT_MAX_USES


def create(conn, team_id: str, created_by: str, *, role: str,
           ttl_ms: int | None = None, max_uses: int | None = None,
           note: str | None = None) -> Minted:
    """Mint a code for `team_id`. The caller has already been checked as admin.

    The plaintext is returned and not stored. `routes/teams.py` puts it in the
    201 body and it exists nowhere else on this side after that -- which is
    why `cci team invite` says so at the moment it prints it, rather than
    leaving somebody to discover it when they look for it again.
    """
    now = now_ms()
    plain, digest = mint()
    invite_id = ids.new_id(ids.INVITE)
    expires_at = now + clamp_ttl(ttl_ms)
    uses = clamp_uses(max_uses)
    conn.execute(
        """INSERT INTO team_invite
               (invite_id, team_id, code_hash, role, created_by, created_at,
                expires_at, max_uses, uses, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)""",
        (invite_id, team_id, digest, role, created_by, now, expires_at, uses, note),
    )
    return Minted(invite_id=invite_id, code=plain, team_id=team_id, role=role,
                  created_at=now, expires_at=expires_at, max_uses=uses, note=note)


def redeem(conn, plain: str, account_id: str) -> Joined | None:
    """Join the team this code names, as `account_id`. `None` if it may not be.

    `None` covers unknown, malformed, revoked, expired and exhausted, and the
    caller must not tell them apart -- see this module's docstring. There is
    deliberately no second return value saying which it was, because a
    `reason` field is how that distinction leaks back out six months later.

    THE ROW LOCK IS LOAD-BEARING. `SELECT ... FOR UPDATE` serialises two
    redemptions of the same code: without it both transactions read `uses = 0`
    against a single-use invite, both write 1, and two people join on one
    seat. The `CHECK (uses <= max_uses)` in migration 005 would catch that
    particular race, but only by failing one request with a constraint
    violation rather than a clean 404, and it would not catch the general case
    of `max_uses = 2` letting three people in.
    """
    if not plain:
        return None
    row = conn.execute(
        """SELECT i.invite_id, i.team_id, i.code_hash, i.role, i.expires_at,
                  i.max_uses, i.uses, i.revoked_at, t.name AS team_name
           FROM team_invite i JOIN team t ON t.team_id = i.team_id
           WHERE i.code_hash = %s
           FOR UPDATE OF i""",
        (hash_code(plain),),
    ).fetchone()
    if row is None:
        return None
    # Belt and braces over the indexed lookup above; see `matches`.
    if not matches(row["code_hash"], plain):
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] <= now_ms():
        return None

    # Already on the team: a no-op that consumes nothing and CHANGES NOTHING.
    #
    # Not an error, because the honest cause is a retried `cci team join` on a
    # flaky connection and failing that would be unhelpful. Not a role change
    # either, and that half matters: if redeeming an `admin` code promoted an
    # existing member, then a leaked admin code would be a self-promotion
    # route for everybody already inside. Promotion stays an admin action on
    # an admin route, where an admin decides it about a named person.
    existing = conn.execute(
        "SELECT role FROM team_member WHERE team_id = %s AND account_id = %s",
        (row["team_id"], account_id),
    ).fetchone()
    if existing is not None:
        return Joined(team_id=row["team_id"], team_name=row["team_name"],
                      role=existing["role"], invite_id=row["invite_id"],
                      already_member=True)

    if row["uses"] >= row["max_uses"]:
        return None

    now = now_ms()
    # A seat is spent per JOIN, not per distinct person. An account that
    # joined, was removed by an admin and came back through the same code
    # spends a second seat -- so a single-use invite is not a standing back
    # door that survives the removal. Removal being effective is worth more
    # than the convenience of a free rejoin.
    conn.execute(
        "UPDATE team_invite SET uses = uses + 1 WHERE invite_id = %s",
        (row["invite_id"],),
    )
    conn.execute(
        """INSERT INTO team_invite_redemption (invite_id, account_id, redeemed_at)
           VALUES (%s, %s, %s)""",
        (row["invite_id"], account_id, now),
    )
    conn.execute(
        """INSERT INTO team_member (team_id, account_id, role, joined_at, invite_id)
           VALUES (%s, %s, %s, %s, %s)""",
        (row["team_id"], account_id, row["role"], now, row["invite_id"]),
    )
    return Joined(team_id=row["team_id"], team_name=row["team_name"],
                  role=row["role"], invite_id=row["invite_id"], already_member=False)


def listing(conn, team_id: str) -> list[dict]:
    """Every invite on this team, WITHOUT the codes, for an admin to audit.

    There is no code in the output and there cannot be -- only the hash was
    kept. That is a property to be glad of rather than to apologise for: an
    endpoint that could re-show a code would make every admin's session a way
    to recover every live invite on the instance.

    Redemptions come back with actor names, because "who did I let in" is the
    question this list is read to answer and an opaque `account_id` does not
    answer it.
    """
    rows = conn.execute(
        """SELECT i.invite_id, i.role, i.created_at, i.expires_at, i.max_uses,
                  i.uses, i.revoked_at, i.note, i.created_by, a.actor AS created_by_actor
           FROM team_invite i JOIN account a ON a.account_id = i.created_by
           WHERE i.team_id = %s ORDER BY i.created_at DESC""",
        (team_id,),
    ).fetchall()
    if not rows:
        return []
    redemptions = conn.execute(
        """SELECT r.invite_id, r.redeemed_at, r.account_id, a.actor
           FROM team_invite_redemption r JOIN account a ON a.account_id = r.account_id
           WHERE r.invite_id = ANY(%s::text[]) ORDER BY r.redeemed_at""",
        ([r["invite_id"] for r in rows],),
    ).fetchall()

    now = now_ms()
    out = []
    for r in rows:
        mine = [x for x in redemptions if x["invite_id"] == r["invite_id"]]
        out.append({
            "inviteId": r["invite_id"],
            "role": r["role"],
            "createdAt": r["created_at"],
            "createdBy": r["created_by"],
            "createdByActor": r["created_by_actor"],
            "expiresAt": r["expires_at"],
            "maxUses": r["max_uses"],
            "uses": r["uses"],
            "revokedAt": r["revoked_at"],
            "note": r["note"],
            # Derived here rather than left to the client, so the CLI and any
            # other reader agree about what "live" means. Three ways to be
            # dead and a reader that checked two of them would show a code as
            # usable when it is not.
            "active": (r["revoked_at"] is None and r["expires_at"] > now
                       and r["uses"] < r["max_uses"]),
            "redemptions": [
                {"accountId": x["account_id"], "actor": x["actor"],
                 "redeemedAt": x["redeemed_at"]}
                for x in mine
            ],
        })
    return out


def revoke(conn, team_id: str, invite_id: str) -> bool:
    """Kill a code. True if it existed on this team.

    Scoped by `team_id` as well as `invite_id` so an admin of one team cannot
    revoke another team's invite by guessing -- `invite_id` is 128 random bits
    and not guessable, but the scope check costs nothing and means the route's
    correctness does not rest on that.

    Idempotent, and `revoked_at` does not move on a second call: the caller's
    goal is already true, and the first revocation is the one worth recording.
    Same rule as `tokens.revoke`.
    """
    row = conn.execute(
        """UPDATE team_invite SET revoked_at = COALESCE(revoked_at, %s)
           WHERE invite_id = %s AND team_id = %s
           RETURNING invite_id""",
        (now_ms(), invite_id, team_id),
    ).fetchone()
    return row is not None
