-- CC-Insights account server, migration 001: who you are, and how you prove it.
--
-- PORTABILITY CONTRACT, inherited from migrations/001_init.sql with one
-- deliberate difference:
--   * Every timestamp is epoch MILLISECONDS UTC. The client's files say
--     INTEGER and `db.translate_ddl` rewrites that to BIGINT on the way to
--     PostgreSQL, because Postgres INTEGER is int4 (max 2.1e9) and epoch-ms is
--     ~1.79e12. These files say BIGINT outright: this store is PostgreSQL-only,
--     BIGINT is accepted by both engines anyway, and a literal type needs no
--     rewriter standing between the file and the database.
--   * All ids are TEXT. No AUTOINCREMENT, no integer surrogate keys.
--   * Upserts use INSERT ... ON CONFLICT DO UPDATE.
--   * No strftime/julianday. Date bucketing happens in application code.
--
-- METADATA ONLY: no column in this schema holds prompt or response text,
-- tool arguments, or file contents. That is a release blocker, not a style
-- rule. Nothing here is even adjacent to it -- this file is identity -- but
-- the sentence stays at the top of every migration so that the next person to
-- add a column reads it before they do.

CREATE TABLE schema_migrations (
    version    BIGINT PRIMARY KEY,
    applied_at BIGINT NOT NULL
);

-- A person.
--
-- `actor` is the name this account's published rows carry, and it lives here
-- rather than being taken from the request body on purpose. `redact.Session`
-- has an `actor` field that the projection fills from a parameter, and
-- docs/REDACTION.md §3 says the module "has no opinion about where it came
-- from". The server does: if the client chose it, one account could publish
-- work under another person's name, and the team view's entire claim -- that
-- it says who did the work on purpose -- would be unenforced.
--
-- It is separate from the GitHub login on `identity` because a login can be
-- renamed and an account cannot. Published rows keep the actor they were
-- stamped with; `account_id` is what joins them back to a person.
CREATE TABLE account (
    account_id TEXT PRIMARY KEY,
    actor      TEXT NOT NULL UNIQUE,
    created_at BIGINT NOT NULL
);

-- How an account proves it is that account; more than one row per account.
--
-- A table rather than columns on `account` because the decision recorded in
-- docs/ACCOUNTS.md §1 is "GitHub now, email later". One account, several ways
-- to sign in, and no migration on the day the second one arrives -- which is
-- the only reason to pay for the join today.
CREATE TABLE identity (
    identity_id TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    provider    TEXT NOT NULL,        -- 'github' | 'email'
    -- The provider's STABLE user id: GitHub's numeric id, not the login.
    -- Logins are renameable and reusable, so keying on one means the next
    -- person to claim an abandoned login inherits somebody's account.
    subject     TEXT NOT NULL,
    -- The human-readable form of `subject` at the last sign-in. Display only;
    -- never matched on.
    label       TEXT,
    created_at  BIGINT NOT NULL,
    updated_at  BIGINT NOT NULL,
    UNIQUE (provider, subject)
);

CREATE INDEX idx_identity_account ON identity (account_id);

CREATE TABLE team (
    team_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE team_member (
    team_id    TEXT NOT NULL REFERENCES team(team_id),
    account_id TEXT NOT NULL REFERENCES account(account_id),
    role       TEXT NOT NULL,         -- 'member' | 'admin'
    joined_at  BIGINT NOT NULL,
    PRIMARY KEY (team_id, account_id),
    CHECK (role IN ('member', 'admin'))
);

CREATE INDEX idx_team_member_account ON team_member (account_id);

-- Bearer tokens this server issued, stored HASHED.
--
-- A token is password-equivalent: it reads one account's filesystem paths.
-- What is stored is sha256 of the token string, and the plaintext exists only
-- in the response that minted it.
--
-- sha256 rather than bcrypt/argon2, which is the opposite of the advice for
-- passwords, and the reason is that these are not passwords. A token is 256
-- bits from `secrets.token_urlsafe`, so there is no dictionary to attack and
-- no cheaper path than brute-forcing the full keyspace; a slow KDF would buy
-- nothing and would cost a KDF evaluation on every single request, which is
-- every push of a 191,475-row corpus. What a slow hash protects -- a low
-- entropy secret in a stolen dump -- does not exist here.
--
-- Lookup is by hash, so the hash is indexed and must be deterministic: a
-- per-row salt would force a table scan and a comparison against every row.
CREATE TABLE api_token (
    token_id     TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES account(account_id),
    token_hash   TEXT NOT NULL UNIQUE,
    -- What the client called itself at `cci login`. Shown in `cci auth list`
    -- so a person can tell which laptop a token belongs to before revoking it.
    name         TEXT,
    created_at   BIGINT NOT NULL,
    last_used_at BIGINT,
    -- NULL = no clock expiry. A CLI token that expires silently mid-week turns
    -- a working background job into a quiet capture gap, which is the failure
    -- mode `cci doctor` exists because of. Revocation is the control.
    expires_at   BIGINT,
    revoked_at   BIGINT
);

CREATE INDEX idx_api_token_account ON api_token (account_id);

-- One in-flight device authorisation grant. RFC 8628, and docs/ACCOUNTS.md §6:
-- a CLI cannot reliably receive a browser callback, and a local listener on a
-- random port is the fragile version of that.
--
-- Two separate device codes exist and they must not be confused:
--   * `device_code_hash` is OUR code, the one the client polls us with. Hashed
--     for the same reason an api_token is: for the life of the flow it is a
--     bearer credential that can be exchanged for a real token.
--   * `provider_device_code` is GITHUB's, and it never leaves this server.
--     The client never holds a GitHub credential of any kind; see
--     docs/SERVER_API.md §2.
CREATE TABLE device_authorization (
    device_id            TEXT PRIMARY KEY,
    device_code_hash     TEXT NOT NULL UNIQUE,
    user_code            TEXT NOT NULL,
    provider             TEXT NOT NULL,
    provider_device_code TEXT NOT NULL,
    verification_uri     TEXT NOT NULL,
    client_name          TEXT,
    -- Seconds. Raised by 5 whenever GitHub says slow_down, or whenever the
    -- client polls us faster than this. It only ever increases: one impatient
    -- CLI getting the whole instance rate-limited at GitHub means nobody can
    -- sign in, so the back-off has to be sticky rather than per-response.
    interval_s           BIGINT NOT NULL,
    last_polled_at       BIGINT,
    created_at           BIGINT NOT NULL,
    expires_at           BIGINT NOT NULL,
    -- 'pending' | 'complete' | 'denied' | 'expired'. A completed row is kept
    -- so that a replayed poll gets `invalid_device_code` rather than minting a
    -- second token for a code someone found in a shell history.
    status               TEXT NOT NULL,
    completed_at         BIGINT,
    account_id           TEXT REFERENCES account(account_id),
    CHECK (status IN ('pending', 'complete', 'denied', 'expired'))
);

CREATE INDEX idx_device_auth_expires ON device_authorization (expires_at);

-- Repos a person's forge identity was verified to reach, at their last
-- sign-in. docs/ACCOUNTS.md §1: team scope is DERIVED from repo access rather
-- than hand-maintained, because the groups already carry forge/owner/repo.
--
-- `repo_id` is `redact.repo_id(normalized_remote)` -- the same hash the client
-- computes, which is what lets an access list built from the GitHub API join
-- against rows built from a git remote on a laptop.
--
-- The forge token used to build this is discarded in the same request. That is
-- the trade: access is only as fresh as the last `cci login`, and in exchange
-- a database dump contains no credential for anybody's source code.
CREATE TABLE account_repo_access (
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    repo_id     TEXT NOT NULL,
    provider    TEXT NOT NULL,
    verified_at BIGINT NOT NULL,
    PRIMARY KEY (account_id, repo_id)
);

CREATE INDEX idx_account_repo_access_repo ON account_repo_access (repo_id);
