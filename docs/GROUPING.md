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

## Naming: what the UI calls things vs what the code calls them

**The user's model is the correct one, and the code's is a historical
accident.** A *project* is atlas-chat. A path like
`atlas-chat/.claude/worktrees/tenant-restricted` is just where one checkout
of it happens to live. Stage 0 named the on-disk path `project` because
`project_id = hash(root_path)` was the first thing that needed a name, and
everything downstream inherited it.

| concept | UI says | schema / API says |
| --- | --- | --- |
| the logical project (atlas-chat) | **Project** | `project_group`, `group` |
| one on-disk path or worktree | **Path** | `project` |

The UI uses the honest words. The schema keeps its names because
`project_id` is an immutable content hash embedded in every session row —
renaming the table would be churn with no gain, and renaming the *concept*
mid-flight is how half-done renames happen.

**This table is the contract between the two vocabularies.** Anyone reading
`metrics.py` and then the dashboard needs it; do not let the mapping drift.
A single UI surface that says "group" is a bug.

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
   (`atlas-chat/backend` → `atlas-chat`). Longest matching ancestor wins,
   and matching is whole-segment, so `atlas-chat-loam` is never swallowed by
   `atlas-chat`.

   **Never anchor on the home directory, a filesystem root, or anything at or
   above `$HOME`.** `~` is itself a project row on this corpus, because someone
   once ran an agent there for 0.0 h. Read literally, rule 4 makes it the
   ancestor of every otherwise-unclaimed path and sweeps three unrelated real
   projects — `slm-finetune`, `atlas-chat-loam`, `census-pipeline/classification` —
   into one group named after the user. Entry count is not the objective:
   grouping unrelated work is worse than leaving it alone, because the human
   then has to *unpick* it. An honest singleton costs a row; a false grouping
   costs trust in every number on the page.
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

## Measured result on this corpus (2026-09-20)

45 project rows → **14 groups**.

- **atlas-chat: 13 paths, 111.6 h** — the checkout, a `.claude` worktree, two
  subdirectories, and all 8 `~/.t3/worktrees/atlas-chat/*` rows (7 of which
  no longer exist on disk). Previously displayed as 54.5 h.
- **skill-planner: 5 paths, 28.3 h.**
- `platform-monorepo` keeps a stripped Azure URL (`forge = dev.azure.com`,
  no GitHub owner) and no credentials.

Ten paths corpus-wide no longer exist and are placed by shape alone.

*Correction:* an earlier draft of this document said 9 dead
`.t3/worktrees/atlas-chat/*` paths. There are 8 such rows and 7 are dead —
`t3code-42282307` is still on disk and matches rule 1 directly. Caught by the
implementer rather than coded to.
