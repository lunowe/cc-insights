# Project grouping — design

One logical project, many on-disk paths. Worktrees and monorepo subdirectories
are the normal case, not an edge case.

## The problem, measured on the author's corpus (2026-09-20)

> Project, repository and worktree names in this document are invented. The
> hour counts, row counts and rule outcomes are the measured ones — a name
> like `atlas-chat` stands for one real project throughout.

45 project rows are really about 13 projects.

| logical project | rows | shown as | actually |
| --- | --- | --- | --- |
| atlas-chat | 5 (+9 dead worktrees) | 54.5 h | **~110 h** |
| skill-planner | 5 | 17.5 h | **28.3 h** |

`retry-budget-spike` (39.8 h) is `atlas-chat/.claude/worktrees/retry-budget-spike`.
`backend` appears twice, under two different repos. Ten paths no longer exist.

## Naming: what the UI calls things vs what the code calls them

**The user's model is the correct one, and the code's is a historical
accident.** A *project* is atlas-chat. A path like
`atlas-chat/.claude/worktrees/retry-budget-spike` is just where one checkout
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
   and matching is whole-segment, so `atlas-chat-sdk` is never swallowed by
   `atlas-chat`.

   **Never anchor on the home directory, a filesystem root, or anything at or
   above `$HOME`.** `~` is itself a project row on this corpus, because someone
   once ran an agent there for 0.0 h. Read literally, rule 4 makes it the
   ancestor of every otherwise-unclaimed path and sweeps three unrelated real
   projects — `slm-finetune`, `atlas-chat-sdk`, `census-pipeline/classifier` —
   into one group named after the user. Entry count is not the objective:
   grouping unrelated work is worse than leaving it alone, because the human
   then has to *unpick* it. An honest singleton costs a row; a false grouping
   costs trust in every number on the page.
5. **ungrouped** — no group. Renders as a group of one. Legal, not an error.

## Paths belong to a machine, not to this one

Every rule above reads paths, and from v2 on the machine rendering them is
usually not the machine they came from. `src/cc_insights/paths.py` therefore
takes the *flavor* of a path from the string — a drive letter, a UNC prefix,
a leading `/` — never from `os.name`. Asking `os.path` instead is not a
cosmetic bug: on a Mac, `os.path.basename(r"C:\Users\you\Coding\repo")`
returns the whole string, so rule 3 finds no checkout to anchor on and every
project on a colleague's box collapses into one group named after their
whole path. Three consequences worth stating:

- **Windows comparison folds case, POSIX does not.** The filesystems differ,
  so `.Claude\Worktrees` is a rule-3 match on Windows and is not one on a Mac.
- **The `$HOME` rule cannot be looked up for a foreign path.** `anchorable`
  still asks the live OS for the local flavor, and additionally refuses the
  well-known shapes structurally — `/Users/<n>`, `/home/<n>`, `/root`,
  `C:\Users\<n>`, and the parent of each. Without that, the home-directory
  failure described under rule 4 simply arrives over the wire instead.
- **Foreign paths are never stat-ed.** `os.path.isdir` would answer False for
  a live worktree on another host and mark it dead, discarding what the
  machine that owns it learned. The probe skips them and keeps the cache.

Stored `root_path` values are never rewritten: `project_id = hash(root_path)`,
so normalizing a path in place would fork the history. Only comparison is
canonical; storage and display keep the original string.

## The probe cache is a fact about one disk

Because `project_id = hash(root_path)`, two machines with the same layout —
a laptop and a desktop both at `/Users/you/Coding/X`, two CI boxes at
`/home/ci/work` — are **one** `project` row. So the cached probe results live
in `project_probe`, keyed `(project_id, host_id)`, and every reader says which
machine it means:

- **The ladder** (`load_probes`) prefers the local host's row, falling back to
  the most recently probed other one. `path_exists` and `git_common_dir`
  describe a disk and the disk we can act on is this one; `git_remote`
  describes the *repository*, so a colleague's answer is a good stand-in — and
  it is what lets rule 1 group a project this machine never checked out.
- **Writing back** records only what this machine learned, falling back to
  what this machine previously learned. A remote borrowed from another host
  informs the ladder but is never copied into our own row, or first-hand and
  second-hand answers become indistinguishable.
- **"Is this path gone?"** (`PATH_EXISTS_ANY`) is `MAX` across every host.
  Live on any machine means not gone. Deleting a checkout on the laptop must
  not put `(gone)` next to a project the desktop is working in right now —
  that tells the person who is right that they are wrong. `MAX` of no rows is
  still `NULL`, so "never probed" stays distinct from "missing".

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
`t3code-6a3f8d52` is still on disk and matches rule 1 directly. Caught by the
implementer rather than coded to.
