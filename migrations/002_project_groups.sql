-- Project grouping: one logical project, many on-disk paths.
--
-- A project row is an immutable identity keyed on its logged cwd
-- (project_id = hash(root_path)); that must not change, or history already
-- recorded would fork. Grouping is therefore a SEPARATE, MUTABLE layer on top.
-- Regrouping never rewrites a project_id and never touches a span.
--
-- Why this exists: on the author's corpus, 45 project rows collapse to ~13
-- real projects. atlas-chat alone appears 5 times -- the checkout, a
-- .claude worktree, two subdirectories and a .t3 worktree -- and reads 54.5 h
-- when its true total is over 100 h. Worktrees and monorepo subdirectories are
-- the normal case, not an edge case.

CREATE TABLE project_group (
    group_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    -- How this group was established, strongest evidence first:
    --   git_remote     normalized origin URL shared by several checkouts
    --   git_common_dir a worktree resolved to its main repo (no remote)
    --   path_worktree  a known worktree path shape; works when the path is GONE
    --   path_ancestor  the path sits inside another project's root
    --   manual         a human said so. Never overwritten by detection.
    origin     TEXT NOT NULL,
    match_key  TEXT,
    -- Forge metadata. Credentials MUST be stripped from remote_url before it
    -- is stored: real remotes here carry a username, and this database is
    -- designed to be shareable.
    remote_url TEXT,
    forge      TEXT,
    owner      TEXT,
    repo       TEXT,
    web_url    TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (origin, match_key)
);

-- Nullable: an ungrouped project is legal and renders as a group of one.
ALTER TABLE project ADD COLUMN group_id TEXT REFERENCES project_group(group_id);

-- 1 = a human placed this project. `cci group auto` must never move it.
ALTER TABLE project ADD COLUMN group_pinned INTEGER NOT NULL DEFAULT 0;

-- Cached probe results, so detection need not re-stat the filesystem, and so a
-- path that later disappears keeps what was learned while it existed.
ALTER TABLE project ADD COLUMN git_remote     TEXT;
ALTER TABLE project ADD COLUMN git_common_dir TEXT;
ALTER TABLE project ADD COLUMN path_exists    INTEGER;
ALTER TABLE project ADD COLUMN detected_at    INTEGER;

CREATE INDEX idx_project_group ON project (group_id);
