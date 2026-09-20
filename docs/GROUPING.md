# Project grouping — design

One logical project, many on-disk paths. Worktrees and monorepo subdirectories
are the normal case, not an edge case.

## The problem, measured on the author's corpus (2026-09-20)

45 project rows are really about 13 projects.

| logical project | rows | shown as | actually |
| --- | --- | --- | --- |
| atlas-chat | 5 (+9 dead worktrees) | 54.5 h | **~110 h** |
| skill-planner | 5 | 17.5 h | **28.3 h** |

`tenant-restricted` (39.8 h) is `atlas-chat/.claude/worktrees/tenant-restricted`.
`backend` appears twice, under two different repos. Ten paths no longer exist.

## Identity vs grouping — the load-bearing distinction

`project_id = hash(root_path)` is **immutable identity**. It must never change,
or history already recorded forks into duplicates, and a second machine
ingesting the same logs would disagree.

**Grouping is a separate, mutable layer** (`project_group`, `project.group_id`).
Regrouping rewrites no `project_id` and touches no span. It can be re-run,
corrected, or thrown away at any time.

## Detection ladder

Each project takes the **first** rule that matches. Strongest evidence first.

1. **`git_remote`** — `git remote get-url origin`, normalized. Two checkouts
   with the same origin are the same project. Also yields forge/owner/repo.
2. **`git_common_dir`** — `git rev-parse --git-common-dir`. Resolves a worktree
   to its main repo when there is no remote. If that main repo has a remote,
   fold into its rule-1 group instead of making a second one.
3. **`path_worktree`** — known worktree shapes. **Works when the path is gone**,
   which matters: 10 paths on this corpus no longer exist and hold 18 h.
   - `.../<repo>/.claude/worktrees/<name>` → `<repo>`
   - `~/.t3/worktrees/<repo>/<name>` → `<repo>`
   - `~/conductor/workspaces/<repo>/<name>` → `<repo>`
4. **`path_ancestor`** — the path lies inside another project's `root_path`
   (`atlas-chat/backend` → `atlas-chat`). Longest matching ancestor wins.
5. **ungrouped** — no group. Renders as a group of one. Legal, not an error.

A rule-3 or rule-4 match should adopt the ancestor's rule-1 group where one
exists, so a worktree and its parent checkout land in the *same* group rather
than two same-named ones.

## Manual override

`project.group_pinned = 1` means a human placed this project.
**`cci group auto` must never move a pinned project**, must never delete a
group that has pinned members, and must report how many it skipped.

This is the escape hatch: automatic detection will be wrong sometimes, and the
human's answer is final.

## Credentials

Real remotes carry a username:
`https://northwind-ops@dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo`

**Strip userinfo before storing.** This database is designed to be shareable and
to sync to a multi-machine backend later. Normalization:

- `git@github.com:owner/repo.git` → `https://github.com/owner/repo`
- strip `user@` and any `:password`, strip a trailing `.git` and trailing `/`
- lowercase the host only; a path may be case-sensitive

## Forge metadata — v3 groundwork

From a normalized remote, store `forge` (`github`, `gitlab`, `azure`, …),
`owner`, `repo`, and a browsable `web_url`. Recognize GitHub properly; store
the rest generically rather than guessing.

This is what makes ROADMAP v3 possible: with `owner`/`repo` and a local
`git_common_dir`, agent spans can later be joined to commits and PRs in the
same window. **This package stores the metadata only** — no network calls, no
API tokens, no commit correlation yet.

## Expected result on this corpus

~13 groups. atlas-chat should absorb all 5 live rows plus the 9 dead
`.t3/worktrees/atlas-chat/*` paths. `platform-monorepo` keeps a stripped
Azure URL and no GitHub owner/repo.
