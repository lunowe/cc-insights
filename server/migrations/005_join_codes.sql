-- CC-Insights account server, migration 005: joining a team requires consent.
--
-- `POST /v1/teams/{id}/members` took an `accountId` and added that person.
-- Two things were wrong with it and server/DEPLOY.md flagged both:
--
--   1. NOBODY AGREED. Being put on a roster is not nothing. A team's members
--      see each other's actor names, the repos on the roster, and -- wherever
--      a forge-verified admin rostered a repo -- each other's sessions on it.
--      A person acquiring readers without being asked is the kind of default
--      that is fine until the one time it is not.
--
--   2. IT NEEDED AN ACCOUNT ID THE ADMIN COULD NOT GET. `account_id` is a
--      random opaque handle. There is no directory endpoint and there must
--      not be one -- a lookup from a name or an email to an account id is an
--      enumeration oracle over everyone on the instance. So the only way to
--      use the route was for the joiner to read their own id out of `whoami`
--      and paste it to the admin, which is a worse consent flow than this one
--      and was not enforced as consent anyway.
--
-- Replaced by a join code. An admin mints one, sends it however they like,
-- and the colleague redeems it WITH THEIR OWN BEARER TOKEN. The redemption is
-- the consent, it is recorded, and the account id never has to be discovered
-- by anybody.
--
-- Same portability contract and the same BIGINT note as 001.
--
-- METADATA ONLY: no column here holds prompt or response text, tool arguments
-- or file contents. Nothing in this file is adjacent to it -- this is
-- membership -- but the sentence stays at the top of every migration so the
-- next person to add a column reads it before they do.

-- ---------------------------------------------------------------------------
-- A join code, stored the same way a bearer token is: hashed.
-- ---------------------------------------------------------------------------
--
-- A join code IS a bearer secret. It does not authenticate anybody, but
-- whoever holds it plus any account on this instance can put themselves on a
-- roster and read other people's agent time. That makes it password-
-- equivalent for the purposes of storage, and `tokens.py`'s discipline
-- applies unchanged:
--
--   * The plaintext exists exactly twice -- in the 201 that minted it, and
--     wherever the admin pasted it. It is never stored and never logged.
--   * sha256 rather than bcrypt/argon2, for the reason in `tokens.py`: the
--     code is 256 bits from `secrets.token_urlsafe(32)`, so there is no
--     dictionary and no attack cheaper than enumerating the keyspace. A slow
--     KDF would buy nothing and would cost a table scan, because lookup is BY
--     HASH and a per-row salt makes an indexed lookup impossible.
--   * UNIQUE on the hash, so a redemption resolves to at most one team. Two
--     invites that hashed alike would make "which team did I just join"
--     depend on row order.
--
-- WHY THE CODE IS LONG AND OPAQUE RATHER THAN SHORT AND TYPEABLE.
-- `device_authorization.user_code` is `43CA-9AAA`, and that is correct there:
-- it is read off one screen and typed into another within minutes, it is
-- bound to a single in-flight flow, and GitHub throttles the far side. A join
-- code is the opposite on every count -- it is pasted into Slack, it lives
-- for days, and THERE IS NO ATTEMPT THROTTLE ON THIS SERVER to make guessing
-- expensive. With no throttle the only defence is entropy, so the code is the
-- full 256 bits and nobody is expected to type it.
CREATE TABLE team_invite (
    invite_id   TEXT PRIMARY KEY,
    team_id     TEXT NOT NULL REFERENCES team(team_id),
    -- sha256 of the plaintext. The plaintext is not here and cannot be
    -- recovered; `cci team invite` says so at the moment it prints it.
    code_hash   TEXT NOT NULL UNIQUE,
    -- The role the redeemer lands in. Stamped at MINT time, not at redeem
    -- time, so the admin's intent is what is recorded and a redeemer cannot
    -- ask for more than the code was cut for.
    role        TEXT NOT NULL CHECK (role IN ('member', 'admin')),
    -- Half the audit trail: who let this person in. NOT NULL, because an
    -- invite with no author is a door with no record of who opened it, and
    -- "how did they get in" is the whole question this table exists to
    -- answer. The other half is `team_invite_redemption`.
    created_by  TEXT NOT NULL REFERENCES account(account_id),
    created_at  BIGINT NOT NULL,
    -- NOT NULL, unlike `api_token.expires_at`. A token that expires mid-week
    -- turns a working background job into a silent capture gap, which is why
    -- that column is nullable and revocation is the control there. An invite
    -- is the opposite: it is in flight for minutes and then sits in a chat
    -- log forever, and an admin will not remember to revoke it. So every
    -- invite has a deadline and there is no way to mint one without.
    expires_at  BIGINT NOT NULL,
    -- Default 1 at the call site. A code that lets in one person is a code
    -- whose blast radius, when the channel it was pasted into turns out to be
    -- wider than the admin thought, is one person.
    max_uses    BIGINT NOT NULL CHECK (max_uses >= 1),
    uses        BIGINT NOT NULL DEFAULT 0 CHECK (uses >= 0),
    revoked_at  BIGINT,
    -- What the admin called it ("for the design contractors"). Display only,
    -- and free text, so it is shown to team admins and to nobody else.
    note        TEXT,
    -- `uses` may never pass `max_uses`. Enforced here rather than only in the
    -- handler because the handler is one path in and the database is all of
    -- them: a concurrent pair of redemptions that both read `uses = 0` would
    -- otherwise both write 1 and both be let in. The handler takes a row lock
    -- as well; this is what holds if a future one forgets to.
    CHECK (uses <= max_uses)
);

CREATE INDEX idx_team_invite_team ON team_invite (team_id);

-- The other half of the audit trail: who walked through, and when.
--
-- Separate from `team_member` rather than a column on it, because the two
-- answer different questions and a leaver must not erase the first.
-- `team_member` is "who is on this team now" and the row goes away when
-- somebody leaves; this is "how did anybody ever get in", and it is append
-- only. Somebody joining, reading a week of the team's data and leaving is
-- precisely the sequence an admin needs to be able to reconstruct afterwards.
CREATE TABLE team_invite_redemption (
    invite_id   TEXT NOT NULL REFERENCES team_invite(invite_id),
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    redeemed_at BIGINT NOT NULL,
    -- Append only, so the timestamp is part of the key. An account that
    -- joined, was removed and came back through the same code has two rows
    -- and both are the truth; keying on (invite, account) alone would
    -- overwrite the first visit with the second, which is the one an admin
    -- reconstructing an incident most wants to see.
    --
    -- A same-millisecond duplicate cannot arise: the first redemption makes
    -- the account a member, and `redeem` short-circuits on an existing
    -- membership without touching this table.
    PRIMARY KEY (invite_id, account_id, redeemed_at)
);

CREATE INDEX idx_team_invite_redemption_account
    ON team_invite_redemption (account_id);

-- Which invite a current membership arrived through, where one did.
--
-- NULL for the account that created the team -- there was no invite, and
-- `create_team` makes them an admin in the same transaction. Nullable rather
-- than a synthetic self-invite because inventing a row to satisfy a NOT NULL
-- would put a code-shaped record in the table for a code that never existed.
ALTER TABLE team_member ADD COLUMN invite_id TEXT REFERENCES team_invite(invite_id);
