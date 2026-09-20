# Redaction — design

> Measured on the author's corpus, 2026-09-20, by `docs/probes/leakage.py`.
> Run that script against your own database before trusting a number here.
>
> **The numbers are real; the names are not.** Every path, repository,
> organisation and branch used as an example below has been replaced with an
> invented one before publication — the same substitution this document
> argues for. The counts, ratios and attack results are the measured ones and
> have not been adjusted, so an example reading
> `/Users/demo/Coding/atlas-chat` stands for a real path of the same shape
> that really was recovered.

`cci sync` already moves everything between one person's machines, paths
included, and that is correct — it is all the same person's disk. This
document is about the next step, where a *second person* can see some of it,
and it exists because the roadmap says the design has to come before any data
leaves a laptop rather than being bolted on after.

## 0. The finding that constrains everything else

**Publishing `project_id` publishes `root_path`.**

`project_id = sha256(root_path)[:32]`. It is not a secret derived from the
path; it is the path, run through a function anyone can also run. A colleague
who guesses a path can hash the guess and compare.

The probe builds guesses from public knowledge only — your username (in every
commit you have ever pushed), eight conventional parent directory names, and
the repo names from the git remotes, which anyone with repo access already
has. On this corpus:

```
project rows            50
paths guessed          555
ids confirmed by guess  10   →  20% of the corpus recovered from the id alone
```

Recovered outright: `/Users/demo`, `/Users/demo/Coding/atlas-chat`,
`…/pinecrest`, `…/glyphwright`, `…/harbor-cli`, and five more. A longer word list recovers
more; nothing about 555 guesses is a limit.

So three tempting designs are already dead:

- **"Ship the ids, withhold the paths."** The ids *are* the paths.
- **"Hash the paths before publishing."** Same function, same attack.
- **"Salt the hash."** A salt breaks the attack and also breaks the one
  property the whole schema is built on — `docs/ROADMAP.md` opens by noting
  that every id is a content hash so the same row computes the same id on
  every machine. A per-person salt makes two people's rows for one repo
  un-mergeable, which is the entire point of a team view.

The conclusion is not a better hash. It is that **path-derived identity cannot
cross the boundary at all**, and published rows must be keyed by something
that is already public on the other side.

## 1. The boundary is repo access

There is exactly one thing in this data that already knows who may see it: the
git remote. `docs/ROADMAP.md` reaches the same place from the auth direction —
groups carry `forge`/`owner`/`repo`, so a team's scope can be *derived* from
repo access rather than hand-maintained. If you can see the repo, you can see
the agent time spent on it.

That gives the rule:

> **A row may be published only if it belongs to a repo, and only to people
> who can already see that repo.**

Everything that follows is a consequence. A branch name is publishable —
anyone with repo access can `git branch -r`. An owner, a repo name, a forge
URL: same. A local filesystem path is not, because repo access says nothing
about it.

And work with **no** remote is not "redact harder" — there is no question
"may Bob see this?" that anything in the data can answer. It stays local.
Silently dropping it would misreport totals, so it is withheld *and counted*:
`cci privacy` reports the hours that never leave.

The cost is small, because the remote-less work is small:

```
behind a remote     29 projects   178.6 h   92%
no remote at all    21 projects    14.7 h    8%
```

92% of active time has an access boundary to inherit. The 8% is scratch
directories, one-off Codex sessions and this tool's own pre-first-push
checkout.

## 2. Redact before it leaves, not on the way out

The projection runs **on the laptop**. The shared database physically never
receives a path.

The alternative — store everything centrally and filter at query time — fails
the moment anything goes wrong: one API bug, one misconfigured role, one
`psql` session, one backup, and the paths are out. There is no un-leaking.

This is the same discipline `PROMPT.md` § Non-negotiables already applies to
prompt and response text: not "filtered from responses" but *never stored*.
The reason that rule has held is that it is enforced at the point of writing.
Redaction gets the same treatment, for the same reason.

> **The shared database cannot leak what it never received.**

## 3. What crosses, and in what shape

Published rows attach to the **repo**, not to the checkout. That is not only
safer, it matches what the UI already calls a project: `docs/GROUPING.md`
exists because one logical project has many on-disk paths, and the per-path
granularity *is* the private part.

```
repo      repo_id = hash(normalized_remote), forge, owner, repo, web_url, name
session   session_id, repo_id, actor, source, git_branch,
          started_at, ended_at, active_ms, event_count
span      span_id, session_id, thread_role, started_at, ended_at, event_count
```

`repo_id` is keyed on the normalized remote — already credential-stripped by
`grouping.normalize_remote`, and already known to everyone on the other side
of the boundary. Hashing it is for a stable key, not for secrecy, so the
confirmation attack of §0 does not apply: there is nothing to confirm.

`session_id` and `span_id` are safe as they stand. They hash a log UUID, not a
path, so they are unguessable and stay stable across publications — which is
what keeps publishing idempotent, exactly like sync.

`thread_role` is the three-bucket partition `stats.py` already computes
(`human` / `autonomous` / `unattended_root`). It travels as a label so the
partition survives aggregation; recomputing it centrally would need
`is_subagent` and `attended`, and those are fine to send, but the label is
what every consumer actually wants.

`actor` replaces `host_id`. `host_id` is a UUID, so it is not *readable*, but
it silently links one person's machines into one identity, and a team view
should say who did the work on purpose rather than by side effect. It comes
from auth, which does not exist yet — so the projection takes it as a
parameter and `redact.py` has no opinion about where it came from.

Dropped outright, with reasons in `redact.FIELDS`: every path
(`root_path`, `cwd`, `ingest_file.path`), `project.name` and `project_id`
(both path-derived), `hostname`, `cli_version` (a fingerprint that answers no
question anyone is asking), and the whole `project_probe` table, which is a
description of a disk.

## 4. Why a classification table, not a denylist

`redact.FIELDS` classifies **every column in the schema** as `PUBLIC`,
`PRIVATE` or `DERIVED`, each with a reason. A test fails when the schema gains
a column nobody classified.

A denylist is the wrong shape here: it is a list of the leaks someone thought
of, and the next migration adds a column that is not on it. The probe makes
the point concretely. Scanning this corpus for "sensitive-sounding" words
finds four:

```
feat/restricted-org-dbs
retry-budget-spike
scratchpad
tmp
```

Two are real signals; two are noise. And no word list could know that a
directory named after a client is the sensitive one while a directory named
after a colour is not. **The tool cannot classify your secrets, so the default
has to be closed** — a new column is withheld until a human says otherwise,
and the test is what forces the human to look.

For scale, the same probe shows why per-field judgement is not optional:

```
root_path carrying your username     49/50  (98%)
session.cwd carrying your username  319/320 (100%)
```

## 4b. The audit has two severities, and that took three tries

`redact.audit` re-reads the finished projection and looks for anything local
in it. Writing it against the real corpus was instructive, because the first
two versions were useless in the same way:

1. **Substring match on every path segment — 24 findings, all wrong.** The
   username `demo` is a path segment in `/Users/demo/Coding/…` *and* the
   GitHub owner in `https://github.com/demo/harbor-cli`. The second is public by
   the rule in §1.
2. **Whole-token match on every path segment — 160 findings, all wrong.**
   `source = 'codex'` was flagged against `~/.codex`. A branch called
   `t3code/frontend-streaming-perf` was flagged against a directory called
   `frontend`.

The lesson is §4 arriving from the other direction. The tool cannot tell which
of your words are the secret ones, so an audit that guesses produces noise —
and **a check nobody can satisfy is a check everybody learns to skip**, which
is the only real failure mode a release blocker has.

So it asserts only what it can prove, and splits the rest out:

- **`leaks`** — blocking. A full local path, a `project_id` (which *is*
  sha256 of a path), this machine's username or hostname as a whole token.
  Each of these is a defect, not a judgement call.
- **`warnings`** — shown, never blocking. The directory name of a *withheld*
  project appearing as a token in published text. Worth a glance, because
  that is where a client name would live; not worth a veto, because on this
  corpus it fires on `frontend` — `~/Coding/CC-Insights/frontend` is withheld
  (this tool is not pushed yet) and the published branch
  `t3code/frontend-streaming-perf` contains the word. Nothing leaked. Two
  unrelated things are both called frontend.

Two exemptions fell out of the same exercise, and both are the rule in §1
doing its job:

- A directory *inside* a publishable repo is not secret from someone who can
  check that repo out. Only withheld projects contribute names.
- The `actor` field names the person on purpose. Auditing it against "things
  that identify a person" flags the one value that is deliberate.

## 5. What this does not solve

Stated plainly, because a redaction design that claims completeness is worse
than one with a known edge.

- **Aggregates must be computed inside the viewer's scope.** A precomputed
  "Alice: 40 h this week" spanning repos Bob cannot see leaks their existence
  the moment Bob sees the total. This is a constraint on the future query
  layer, not something the projection can enforce, and it needs to be in the
  API's tests from its first commit.
- **Timing is a side channel.** Published spans show when someone was *not*
  working on a visible repo. Gaps correlate with invisible work. Reducing
  granularity (daily buckets) would blunt it and would also destroy the
  parallelism figures that are the point of the tool. Not solved; named.
- **A repo with one contributor is not anonymous**, and should not pretend to
  be. Within scope that is fine by definition — you were granted access to the
  repo, and the person's work on it is the thing you were granted.
- **Branch names are public within scope but are still free text.**
  `feat/restricted-org-dbs` is publishable under the rule in §1 and may
  still be more than its author meant to say. A per-repo opt-out for branch
  names is cheap and should exist before the first team ships.
- **Revocation is not retroactive.** If repo access is removed, previously
  published rows are still in someone's cache or screenshot. Standard for any
  access system; worth saying out loud since the data is about people.

## 6. Status

Implemented: the classification table (110 columns), the schema-coverage
guard, `repo_id`, the projection, the two-severity audit, and `cci privacy`.
On the author's corpus the audit is clean, with two word-collision warnings.

Not implemented, and deliberately: accounts, teams, OAuth, a tenant column, a
publish transport. Those follow, and they now have a shape to fit into —
`actor` is a parameter, the boundary is repo access, and nothing downstream
has to trust a filter.

Two things whoever builds teams must not skip, both from §5: aggregates have
to be computed inside the viewer's scope from the API's first commit, and
branch names need a per-repo opt-out before the first team ships.
