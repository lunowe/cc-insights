-- CC-Insights schema, migration 001.
--
-- PORTABILITY CONTRACT (SQLite now, PostgreSQL later):
--   * Every timestamp is epoch MILLISECONDS UTC, stored as INTEGER here and as
--     BIGINT in PostgreSQL. Postgres INTEGER is int4 (max 2.1e9); epoch-ms is
--     ~1.79e12 and overflows it. Same applies to byte counts.
--   * All ids are TEXT. No AUTOINCREMENT, no integer surrogate keys.
--   * Upserts use INSERT ... ON CONFLICT DO UPDATE (valid in both engines).
--   * No strftime/julianday. Date bucketing happens in application code.
--
-- METADATA ONLY: no column in this schema holds prompt or response text,
-- tool arguments, or file contents. That is a release blocker, not a style rule.

CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

CREATE TABLE host (
    host_id    TEXT PRIMARY KEY,
    hostname   TEXT NOT NULL,
    os         TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE project (
    project_id TEXT PRIMARY KEY,
    root_path  TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL
);

-- A session is the user-facing unit of work: one root conversation.
CREATE TABLE session (
    id          TEXT PRIMARY KEY,
    native_id   TEXT NOT NULL,
    source      TEXT NOT NULL,
    host_id     TEXT NOT NULL REFERENCES host(host_id),
    project_id  TEXT REFERENCES project(project_id),
    cwd         TEXT,
    git_branch  TEXT,
    cli_version TEXT,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    active_ms   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (host_id, source, native_id)
);

-- A thread is one serial stream of events. The main conversation is a root
-- thread (parent_thread_id IS NULL); subagents are child threads.
--   Codex   : threads are explicit -- one log file each, payload.id is the
--             thread id, payload.session_id the root, source.subagent marks it.
--   Claude  : only the root thread has events; subagent threads are DERIVED
--             from Agent tool_use -> tool_result pairs and carry no events.
-- Modelling both as threads is what lets one timeline render both sources.
CREATE TABLE thread (
    id               TEXT PRIMARY KEY,
    native_id        TEXT NOT NULL,
    session_id       TEXT NOT NULL REFERENCES session(id),
    parent_thread_id TEXT REFERENCES thread(id),
    is_subagent      INTEGER NOT NULL DEFAULT 0,
    agent_name       TEXT,
    started_at       INTEGER NOT NULL,
    ended_at         INTEGER NOT NULL,
    event_count      INTEGER NOT NULL DEFAULT 0,
    active_ms        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, native_id)
);

-- native_event_id must be unique WITHIN ITS SESSION. It is the dedup key:
--   Claude : the event `uuid`. Resumed sessions replay history into new files,
--            so ~1300 events legitimately appear twice; this collapses them.
--   Codex  : "<thread_id>:<ordinal>". `ordinal` alone is file-scoped and
--            restarts at 0 in every thread -- keying on it drops 8709 events.
-- Fallback for events with neither: sha256 of the raw line.
CREATE TABLE event (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES session(id),
    thread_id          TEXT NOT NULL REFERENCES thread(id),
    native_event_id    TEXT NOT NULL,
    ts                 INTEGER NOT NULL,
    ordinal            INTEGER,
    kind               TEXT NOT NULL,
    model              TEXT,
    tool_name          TEXT,
    tool_use_id        TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    UNIQUE (session_id, native_event_id)
);

-- Derived: maximal runs of events whose consecutive gaps are <= idle_threshold.
-- A gap ABOVE the threshold contributes zero time -- it is not capped-and-counted.
CREATE TABLE span (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES session(id),
    thread_id   TEXT NOT NULL REFERENCES thread(id),
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    attended    INTEGER
);

CREATE TABLE ingest_file (
    host_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    source      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    mtime_ms    INTEGER NOT NULL,
    bytes_read  INTEGER NOT NULL,
    lines_read  INTEGER NOT NULL,
    last_ingest INTEGER NOT NULL,
    PRIMARY KEY (host_id, path)
);

CREATE INDEX idx_event_session_ts ON event (session_id, ts);
CREATE INDEX idx_event_thread_ts  ON event (thread_id, ts);
CREATE INDEX idx_event_ts         ON event (ts);
CREATE INDEX idx_span_time        ON span (started_at, ended_at);
CREATE INDEX idx_span_session     ON span (session_id);
CREATE INDEX idx_thread_session   ON thread (session_id);
CREATE INDEX idx_session_started  ON session (started_at);
CREATE INDEX idx_session_project  ON session (project_id);
