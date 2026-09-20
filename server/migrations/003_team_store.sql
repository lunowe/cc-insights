-- CC-Insights account server, migration 003: the team store.
--
-- The redacted projection and nothing else. Its shape is not a design
-- decision taken here -- it is `redact.Repo`, `redact.Session` and
-- `redact.Span`, which docs/REDACTION.md §3 settled: keyed on `repo_id`,
-- carrying an `actor`, with no path anywhere.
--
-- NO PATH COLUMN EXISTS IN THIS FILE. Not nullable and unused: absent. There
-- is no `root_path`, no `cwd`, no `project_id`, no `git_common_dir`, no
-- `match_key`, no `hostname`, no `native_id`. docs/REDACTION.md §2 gives the
-- reason in one line -- "the shared database cannot leak what it never
-- received" -- and the corollary is that a column which exists is a column
-- something can eventually be written into. A nullable `root_path` here would
-- be one careless INSERT away from undoing the whole projection, and no code
-- review catches the one that slips through. The schema is the enforcement;
-- `test_team_store_has_no_path_column` reads the live catalog and fails the
-- build if that stops being true.
--
-- Same portability contract and the same BIGINT note as 001.
--
-- METADATA ONLY: no prompt text, response text, tool arguments or file
-- contents. `event` is not published at all -- 187k rows whose analytical
-- value spans already carry, and whose `tool_name`/`model` is a finer picture
-- of a person than a team view has any business holding.

-- One repository, keyed on the normalized git remote.
--
-- `repo_id = redact.repo_id(normalized_remote)` is hashed for a stable,
-- fixed-width key and NOT for secrecy. The input is already public on the far
-- side of the boundary, so the confirmation attack of docs/REDACTION.md §0 --
-- which recovered 20% of the author's project ids from guessed paths -- has
-- nothing to confirm. That is the entire difference between this key and
-- `project_id`, and the reason publication re-keys instead of filtering.
--
-- Deliberately not owned by anyone. Two people on the same repo compute the
-- same id from the same remote, which is what makes a team view of one repo
-- one row rather than two.
CREATE TABLE published_repo (
    repo_id    TEXT PRIMARY KEY,
    remote_url TEXT NOT NULL,
    forge      TEXT,
    owner      TEXT,
    repo       TEXT,
    web_url    TEXT,
    name       TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

-- Who has published into a repo. Exists so that "repos I published to" is an
-- index lookup rather than a scan over every session, because it is one of
-- the three terms of the scope union computed on every single team request.
CREATE TABLE repo_publisher (
    repo_id            TEXT NOT NULL REFERENCES published_repo(repo_id),
    account_id         TEXT NOT NULL REFERENCES account(account_id),
    first_published_at BIGINT NOT NULL,
    last_published_at  BIGINT NOT NULL,
    PRIMARY KEY (repo_id, account_id)
);

CREATE INDEX idx_repo_publisher_account ON repo_publisher (account_id);

-- Which repos a team can see, and the branch-name switch.
--
-- docs/ACCOUNTS.md §5 rule 2: branch names are publishable under the repo
-- rule -- anyone with repo access can run `git branch -r` -- but they are free
-- text, and `feat/restricted-org-dbs` can say more than its author meant.
-- Default on, one switch per repo.
--
-- The switch is per (team, repo) rather than per repo globally, which is a
-- refinement of that sentence rather than a departure from it: a repo can be
-- on two teams' rosters, and an admin of one team flipping a switch that
-- changes what the OTHER team sees is an authority nobody granted them.
-- Off wins when a caller reaches a repo through several teams, because the
-- switch exists to stop a name being shown and "some other team had it on"
-- is not a reason to show it.
CREATE TABLE team_repo (
    team_id                TEXT NOT NULL REFERENCES team(team_id),
    repo_id                TEXT NOT NULL REFERENCES published_repo(repo_id),
    branch_names_published BOOLEAN NOT NULL DEFAULT TRUE,
    added_at               BIGINT NOT NULL,
    added_by               TEXT REFERENCES account(account_id),
    PRIMARY KEY (team_id, repo_id)
);

CREATE INDEX idx_team_repo_repo ON team_repo (repo_id);

-- `redact.Session`, one row.
--
-- `actor` is the published name and `account_id` is the stable identity behind
-- it. Both, because a GitHub login can be renamed: rows keep the actor they
-- were stamped with, and `account_id` is what still joins them to a person
-- afterwards. `actor` is checked against the account's own on the way in --
-- the client does not get to choose whose name its work is published under.
--
-- No `cwd`, no `project_id`, no `host_id`, no `cli_version`, no `native_id`.
-- `git_branch` is NOT here either; it has its own table below.
CREATE TABLE published_session (
    session_id   TEXT PRIMARY KEY,
    repo_id      TEXT NOT NULL REFERENCES published_repo(repo_id),
    account_id   TEXT NOT NULL REFERENCES account(account_id),
    actor        TEXT NOT NULL,
    source       TEXT NOT NULL,
    started_at   BIGINT NOT NULL,
    ended_at     BIGINT NOT NULL,
    active_ms    BIGINT NOT NULL DEFAULT 0,
    event_count  BIGINT NOT NULL DEFAULT 0,
    published_at BIGINT NOT NULL
);

CREATE INDEX idx_pub_session_repo    ON published_session (repo_id, started_at);
CREATE INDEX idx_pub_session_account ON published_session (account_id);
CREATE INDEX idx_pub_session_actor   ON published_session (actor);

-- The branch name, in its own table, and this is the point of the file.
--
-- The switch in `team_repo` has to take effect at READ time: its whole purpose
-- is that somebody can flip it AFTER the rows were published, so a write-time
-- control would do nothing about the names already stored. That is the one
-- deliberate exception to this project's "enforce where it is written"
-- discipline, and it would normally mean a nullable column that every query
-- has to remember to mask -- the exact shape of mistake docs/REDACTION.md §2
-- rejects, because forgetting once is a leak and nothing surfaces it.
--
-- Splitting the column into a table converts "remember to mask" into "remember
-- to join". A query that forgets returns no branch name at all, which is the
-- safe direction to fail in, and the failure is visible in the response rather
-- than silent.
--
-- `repo_id` is duplicated here so the join can be scope-filtered without a
-- second hop through `published_session`; it is written by the server from the
-- parent session row, never accepted from a client.
CREATE TABLE published_session_branch (
    session_id TEXT PRIMARY KEY REFERENCES published_session(session_id),
    repo_id    TEXT NOT NULL REFERENCES published_repo(repo_id),
    git_branch TEXT NOT NULL
);

CREATE INDEX idx_pub_branch_repo ON published_session_branch (repo_id);

-- `redact.Span`.
--
-- `thread_role` is the three-bucket partition `stats.py` computes, travelling
-- as a label so it survives aggregation: recomputing it centrally would need
-- `is_subagent` and `attended`, and the label is what every consumer wants.
-- The CHECK is there because the partition is the point -- a fourth value
-- entering the store would make every breakdown quietly stop adding up, and a
-- typo in a client is exactly how that happens.
--
-- `repo_id` and `account_id` are denormalized from the parent session and
-- written by the server, never by the client. Both are here so that the scope
-- filter in docs/ACCOUNTS.md §5 rule 1 is a predicate on the table being
-- aggregated rather than on a table two joins away. An aggregate that forgets
-- a join is a bug; an aggregate that forgets the scope is a privacy leak, and
-- these two columns mean the scope predicate can never be the thing that got
-- dropped.
CREATE TABLE published_span (
    span_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES published_session(session_id),
    repo_id      TEXT NOT NULL REFERENCES published_repo(repo_id),
    account_id   TEXT NOT NULL REFERENCES account(account_id),
    thread_role  TEXT NOT NULL,
    started_at   BIGINT NOT NULL,
    ended_at     BIGINT NOT NULL,
    event_count  BIGINT NOT NULL DEFAULT 0,
    published_at BIGINT NOT NULL,
    CHECK (thread_role IN ('human', 'autonomous', 'unattended_root'))
);

CREATE INDEX idx_pub_span_repo    ON published_span (repo_id, started_at);
CREATE INDEX idx_pub_span_session ON published_span (session_id);
CREATE INDEX idx_pub_span_account ON published_span (account_id);

-- The hours that never left a laptop.
--
-- docs/ACCOUNTS.md §5: withheld time stays counted. `redact` already reports
-- it -- 8% of the author's corpus, the work with no git remote -- and a
-- dashboard that silently drops part of someone's week is not private, it is
-- wrong, and the person reading it cannot tell the difference.
--
-- There is no time dimension and there cannot be one: the whole definition of
-- withheld work is that it belongs to no repo, so there is nothing to bucket
-- it against and nothing a range filter could narrow. It is a corpus total as
-- of the last publish, and the API says so in the payload rather than letting
-- a renderer assume it matches the range beside it.
--
-- `published_ms` is stored but is served ONLY to its own author. It is
-- corpus-wide across every repo that account published, including repos the
-- caller cannot see, so handing it to a teammate would disclose the magnitude
-- of invisible work -- a weaker form of the leak rule 1 exists to prevent.
-- `withheld_ms` has no such problem: it belongs to no repo by construction, so
-- it cannot reveal one.
CREATE TABLE published_withheld (
    account_id        TEXT NOT NULL REFERENCES account(account_id),
    -- The host the projection was computed on. Per host, because
    -- `redact.publication(conn, actor, host_id=...)` scopes an export to one
    -- machine's own sessions, and two laptops each report their own figure.
    host_id           TEXT NOT NULL,
    withheld_ms       BIGINT NOT NULL,
    withheld_projects BIGINT NOT NULL,
    published_ms      BIGINT NOT NULL,
    as_of             BIGINT NOT NULL,
    PRIMARY KEY (account_id, host_id)
);
