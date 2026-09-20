-- CC-Insights account server, migration 004: publishing is not a grant.
--
-- Two defects, one root. Both come from treating a `repo_id` the CALLER
-- CHOSE as though it were evidence of something.
--
-- 1. `repo_publisher` was term 2 of the scope union, so `POST /v1/team/publish`
--    with `kind=repos` and a guessed id put the caller inside a repo they had
--    no access to. docs/REDACTION.md §0 is explicit that `repo_id` is
--    guessable BY DESIGN -- it is keyed on the remote precisely because the
--    remote is already public on the far side of the boundary -- so a
--    self-asserted id can never be proof of access. `scope.py` now reads the
--    rows an account actually published instead, and that is a ROW-level
--    grant ("you can read what you sent"), never a repo-level one.
--
-- 2. `published_span.repo_id` could disagree with its session's. The comment
--    in 003 claimed it could not; it was wrong, because `_publish_sessions`
--    lets a session move repos and nothing propagated that to the spans
--    already stored. Fixed here in the schema rather than in the handler,
--    because the schema is what holds when a row arrives by some other path.
--
-- Same portability contract and the same BIGINT note as 001.

-- ---------------------------------------------------------------------------
-- 1. What each publisher asserted about a repo, kept per publisher.
-- ---------------------------------------------------------------------------
--
-- `published_repo` stays unowned and shared: two people on one repo compute
-- the same id from the same remote, and that is what makes a team view of one
-- repo one row rather than two. But an UNOWNED row that anyone may write is a
-- row anyone may REWRITE -- the old `ON CONFLICT (repo_id) DO UPDATE` let a
-- stranger replace `name`, `remote_url` and `web_url` for every real member --
-- and an unowned row that anyone may READ is an existence oracle: publish a
-- guessed id with a deliberately wrong name, read it back, and a name that
-- comes back different answers "does anybody here work on acme/skunkworks".
--
-- So the shared row is refreshed only by a caller with VERIFIED access to the
-- repo, and everybody else gets back exactly what they themselves sent. These
-- columns are where "exactly what they themselves sent" is kept.
--
-- They are copies of columns already on `published_repo` and carry the same
-- classification: a remote URL and its parsed parts, all of them public on the
-- far side of the boundary by docs/REDACTION.md §1, and none of them a path.
-- Nullable because a publisher that predates this migration asserted nothing
-- we recorded; the reader falls back to the shared row for those, which is the
-- behaviour they already had.
ALTER TABLE repo_publisher
    ADD COLUMN remote_url TEXT,
    ADD COLUMN forge      TEXT,
    ADD COLUMN owner      TEXT,
    ADD COLUMN repo       TEXT,
    ADD COLUMN web_url    TEXT,
    ADD COLUMN name       TEXT;

-- ---------------------------------------------------------------------------
-- 2. A span cannot disagree with its session. Now actually true.
-- ---------------------------------------------------------------------------
--
-- 003 said the denormalized `repo_id` on `published_span` "cannot disagree
-- with the session". It could. `_publish_sessions` upserts
-- `repo_id = excluded.repo_id`, so a session moves repos whenever a checkout's
-- remote is corrected -- and `redact.py` re-normalizes every remote on every
-- run BY DESIGN, so a fix to `normalize_remote` moves them in bulk. The spans
-- kept the old id. Since every scope predicate in `team_data.py` sits on
-- `sp.repo_id`, a viewer scoped to the OLD repo then read a session belonging
-- to the new one, private `repo_id` included.
--
-- A trigger would also work and is what the handler-level fix would become.
-- A foreign key is better: it is declarative, the database enforces it against
-- every path in including a psql session, and ON UPDATE CASCADE means the
-- correction is not something a handler has to remember to make.
--
-- `session_id` is already the primary key of `published_session`, so this
-- UNIQUE adds no constraint anybody can violate. It exists only to be a
-- referenceable target for the composite key below.
ALTER TABLE published_session
    ADD CONSTRAINT published_session_id_repo_key UNIQUE (session_id, repo_id);

ALTER TABLE published_span
    ADD CONSTRAINT published_span_agrees_with_its_session
    FOREIGN KEY (session_id, repo_id)
    REFERENCES published_session (session_id, repo_id) ON UPDATE CASCADE;

-- Same for the branch name. Its `repo_id` is what the branch-name switch is
-- looked up against, so a stale one would apply the wrong team's switch --
-- and the direction it fails in is disclosure: the OLD repo's switch being
-- on would publish a branch name from a repo whose switch is off.
ALTER TABLE published_session_branch
    ADD CONSTRAINT published_session_branch_agrees_with_its_session
    FOREIGN KEY (session_id, repo_id)
    REFERENCES published_session (session_id, repo_id) ON UPDATE CASCADE;
