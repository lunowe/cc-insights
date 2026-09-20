-- CC-Insights schema, migration 004: the probe cache belongs to a machine.
--
-- Migration 002 cached `git_remote`, `git_common_dir`, `path_exists` and
-- `detected_at` on `project`. That was right while a database held one host.
-- It stops being right the moment two of them share one:
--
--   project_id = hash(root_path), so two machines with the same layout --
--   a laptop and a desktop both at /Users/you/Coding/X, or two CI boxes at
--   /home/ci/work -- are ONE project row. The columns above are facts about
--   ONE disk. Whichever machine probed last would overwrite the other's
--   answer, and `path_exists = 0` from a box where the checkout was deleted
--   would mark the project gone on the machine still working in it.
--
-- So the cache moves to its own table keyed by (project_id, host_id) and every
-- reader resolves it explicitly: the ladder prefers the local host's answer,
-- and "does this path still exist" is answered across hosts -- live on any
-- machine means not gone. See grouping.load_projects and metrics.projects.
--
-- Portability: same contract as 001. TEXT ids, INTEGER epoch-ms, no
-- AUTOINCREMENT, ALTER TABLE ... DROP COLUMN is valid in SQLite >= 3.35 and
-- in PostgreSQL.

CREATE TABLE project_probe (
    project_id     TEXT NOT NULL REFERENCES project(project_id),
    host_id        TEXT NOT NULL REFERENCES host(host_id),
    -- Raw `git remote get-url origin`. Normalized only when it is read.
    git_remote     TEXT,
    git_common_dir TEXT,
    -- Tri-state: NULL = never probed, which is not the same as 0 = gone.
    path_exists    INTEGER,
    detected_at    INTEGER,
    PRIMARY KEY (project_id, host_id)
);

CREATE INDEX idx_project_probe_host ON project_probe (host_id);

-- Backfill, but only when this database has seen exactly one host -- which is
-- every database that exists today, since sync does not ship yet. With two or
-- more hosts there is no honest way to say whose disk the old columns
-- described, and attributing them to the wrong machine is worse than
-- re-probing: `cci group auto` refills the cache on its next run, and the
-- ladder degrades to path-shape rules in the meantime rather than lying.
INSERT INTO project_probe
    (project_id, host_id, git_remote, git_common_dir, path_exists, detected_at)
SELECT p.project_id,
       (SELECT h.host_id FROM host h),
       p.git_remote, p.git_common_dir, p.path_exists, p.detected_at
FROM project p
WHERE (SELECT count(*) FROM host) = 1
  AND (p.git_remote     IS NOT NULL
    OR p.git_common_dir IS NOT NULL
    OR p.path_exists    IS NOT NULL
    OR p.detected_at    IS NOT NULL);

ALTER TABLE project DROP COLUMN git_remote;
ALTER TABLE project DROP COLUMN git_common_dir;
ALTER TABLE project DROP COLUMN path_exists;
ALTER TABLE project DROP COLUMN detected_at;
