-- CC-Insights account server, migration 002: the personal store.
--
-- The full rows -- `~/Coding/<client-name>`, hostnames, every `cwd` an agent
-- ran in -- readable by EXACTLY ONE account. docs/ACCOUNTS.md §2 is explicit
-- that this is not a shared team database with a filter on it, and that there
-- is no admin-can-see-everything mode, because an admin who can read it is a
-- second person. The redacted projection lives in 003 and shares nothing with
-- this file.
--
-- Same portability contract and the same BIGINT note as 001.
--
-- METADATA ONLY: no column here holds prompt or response text, tool arguments
-- or file contents. Every column is a copy of one that already exists in
-- migrations/001_init.sql .. 005 and was classified in `redact.FIELDS`.
--
--
-- WHY EVERY PRIMARY KEY IS COMPOSITE ON account_id.
--
-- docs/ACCOUNTS.md §4 says "every table in the personal store gains
-- `account_id` and every query filters on it", and sketches it as
-- `ALTER TABLE host ADD COLUMN account_id`. Adding the column while leaving
-- the original primary key in place is not enough, and the gap is not
-- theoretical:
--
--   `project_id = sha256(root_path)`, and `project.root_path` is UNIQUE. Two
--   people who both have `/Users/alice/Coding/app` -- or, far more commonly,
--   two CI boxes at `/home/ci/work`, or one person's laptop and desktop with
--   the same layout -- compute the SAME project_id. With a single-column
--   primary key those are one row. Whoever pushes last overwrites the other's
--   `group_id` and `group_pinned`, and the unique index on `root_path` turns
--   a coincidence into a hard error that tells account A something true about
--   account B's disk.
--
-- So the key is (account_id, <id>) everywhere, and every foreign key carries
-- account_id too. That second part is the one that matters: a composite FK
-- makes "this session belongs to a host on a different account" unrepresentable
-- in the database rather than merely unreachable through the API. The
-- application still filters on account_id on every query -- but if it ever
-- forgets, the schema has already made the cross-account join impossible.
--
-- Cross-account overwrite protection at the row level is application logic
-- (`ON CONFLICT ... WHERE`), because a composite key makes the same id under
-- two accounts two legitimate rows, not a conflict.

CREATE TABLE host (
    account_id TEXT NOT NULL REFERENCES account(account_id),
    host_id    TEXT NOT NULL,
    hostname   TEXT NOT NULL,
    os         TEXT,
    first_seen BIGINT NOT NULL,
    last_seen  BIGINT NOT NULL,
    PRIMARY KEY (account_id, host_id)
);

-- `host_id` does NOT become an account id and is not regenerated when a host
-- is claimed. It is baked into every session id, so reassigning it forks the
-- entire history -- config.py has an atomic write guarding exactly that. A
-- host is claimed by an account and keeps its identity, which is why this is
-- an ordinary column here and not a key of its own.

CREATE TABLE project_group (
    account_id TEXT NOT NULL REFERENCES account(account_id),
    group_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    origin     TEXT NOT NULL,
    match_key  TEXT,
    -- Credentials MUST already be stripped by `grouping.normalize_remote`
    -- before this is stored. Real remotes carry a username.
    remote_url TEXT,
    forge      TEXT,
    owner      TEXT,
    repo       TEXT,
    web_url    TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (account_id, group_id),
    UNIQUE (account_id, origin, match_key)
);

CREATE TABLE project (
    account_id   TEXT NOT NULL REFERENCES account(account_id),
    project_id   TEXT NOT NULL,
    -- The leak itself, per redact.FIELDS: username, client names, worktree
    -- names. It is here because this store is one person's own machines and
    -- it is all the same disk. Nothing in migration 003 has a column it could
    -- be copied into.
    root_path    TEXT NOT NULL,
    name         TEXT NOT NULL,
    group_id     TEXT,
    group_pinned BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, project_id),
    UNIQUE (account_id, root_path),
    FOREIGN KEY (account_id, group_id) REFERENCES project_group(account_id, group_id)
);

CREATE INDEX idx_project_group ON project (account_id, group_id);

-- Per (project, host), not per project: migration 004 of the client schema
-- explains why. These columns are facts about ONE disk, and `path_exists = 0`
-- from a box where the checkout was deleted would otherwise mark the project
-- gone on the machine still working in it.
CREATE TABLE project_probe (
    account_id     TEXT NOT NULL REFERENCES account(account_id),
    project_id     TEXT NOT NULL,
    host_id        TEXT NOT NULL,
    git_remote     TEXT,
    git_common_dir TEXT,
    -- Tri-state: NULL = never probed, which is not the same as 0 = gone.
    path_exists    BIGINT,
    detected_at    BIGINT,
    PRIMARY KEY (account_id, project_id, host_id),
    FOREIGN KEY (account_id, project_id) REFERENCES project(account_id, project_id),
    FOREIGN KEY (account_id, host_id)    REFERENCES host(account_id, host_id)
);

CREATE INDEX idx_project_probe_host ON project_probe (account_id, host_id);

CREATE TABLE session (
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    id          TEXT NOT NULL,
    native_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    host_id     TEXT NOT NULL,
    project_id  TEXT,
    cwd         TEXT,
    git_branch  TEXT,
    cli_version TEXT,
    started_at  BIGINT NOT NULL,
    ended_at    BIGINT NOT NULL,
    event_count BIGINT NOT NULL DEFAULT 0,
    active_ms   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, id),
    UNIQUE (account_id, host_id, source, native_id),
    FOREIGN KEY (account_id, host_id)    REFERENCES host(account_id, host_id),
    -- MATCH SIMPLE (the default) means a NULL project_id satisfies this
    -- without a parent, which is what we want: a session whose cwd never
    -- resolved to a project is legal and carries real time.
    FOREIGN KEY (account_id, project_id) REFERENCES project(account_id, project_id)
);

CREATE INDEX idx_session_started ON session (account_id, started_at);
CREATE INDEX idx_session_project ON session (account_id, project_id);
CREATE INDEX idx_session_host    ON session (account_id, host_id);

CREATE TABLE thread (
    account_id       TEXT NOT NULL REFERENCES account(account_id),
    id               TEXT NOT NULL,
    native_id        TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    parent_thread_id TEXT,
    is_subagent      BIGINT NOT NULL DEFAULT 0,
    agent_name       TEXT,
    started_at       BIGINT NOT NULL,
    ended_at         BIGINT NOT NULL,
    event_count      BIGINT NOT NULL DEFAULT 0,
    active_ms        BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, id),
    UNIQUE (account_id, session_id, native_id),
    FOREIGN KEY (account_id, session_id) REFERENCES session(account_id, id),
    -- DEFERRABLE, and the only deferred constraint in the schema. A subagent
    -- can be nested several levels deep, so a batch of threads contains
    -- children whose parents are elsewhere in the same batch;
    -- `sync.order_by_parent` sorts them on the client, and deferring to commit
    -- means a client that gets that wrong gets correct data rather than an
    -- intermittent failure that only reproduces on someone else's machine.
    FOREIGN KEY (account_id, parent_thread_id) REFERENCES thread(account_id, id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE INDEX idx_thread_session ON thread (account_id, session_id);

CREATE TABLE event (
    account_id            TEXT NOT NULL REFERENCES account(account_id),
    id                    TEXT NOT NULL,
    session_id            TEXT NOT NULL,
    thread_id             TEXT NOT NULL,
    native_event_id       TEXT NOT NULL,
    ts                    BIGINT NOT NULL,
    ordinal               BIGINT,
    kind                  TEXT NOT NULL,
    model                 TEXT,
    tool_name             TEXT,
    -- `tool_use_id` is an identifier, not an argument. No column in this table
    -- holds what the tool was CALLED WITH; see the metadata-only note above.
    tool_use_id           TEXT,
    input_tokens          BIGINT,
    output_tokens         BIGINT,
    cache_read_tokens     BIGINT,
    cache_write_tokens    BIGINT,
    -- Of which: the part that bought a one-hour TTL. NULL means the TTL is
    -- unknown, which is distinguishable from 0 = all five-minute writes.
    -- Present here and OPTIONAL on the wire, because `sync.TABLES` has not
    -- listed it since migration 005 added it -- see personal_schema.py.
    cache_write_1h_tokens BIGINT,
    PRIMARY KEY (account_id, id),
    UNIQUE (account_id, session_id, native_event_id),
    FOREIGN KEY (account_id, session_id) REFERENCES session(account_id, id),
    FOREIGN KEY (account_id, thread_id)  REFERENCES thread(account_id, id)
);

CREATE INDEX idx_event_session_ts ON event (account_id, session_id, ts);
CREATE INDEX idx_event_thread_ts  ON event (account_id, thread_id, ts);
CREATE INDEX idx_event_ts         ON event (account_id, ts);

CREATE TABLE span (
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    id          TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    thread_id   TEXT NOT NULL,
    started_at  BIGINT NOT NULL,
    ended_at    BIGINT NOT NULL,
    event_count BIGINT NOT NULL,
    attended    BIGINT,
    PRIMARY KEY (account_id, id),
    FOREIGN KEY (account_id, session_id) REFERENCES session(account_id, id),
    FOREIGN KEY (account_id, thread_id)  REFERENCES thread(account_id, id)
);

CREATE INDEX idx_span_time    ON span (account_id, started_at, ended_at);
CREATE INDEX idx_span_session ON span (account_id, session_id);
