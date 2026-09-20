# Accounts and teams — design

> Status: built. The server lives in `server/`; its wire contract is
> `docs/SERVER_API.md` and that document is frozen. §4 carries a list of
> four things this design got wrong, found by implementing it.
> `docs/REDACTION.md` is the prerequisite and is done; this document is what
> consumes it.

Two asks arrived together, and they turned out to be one piece of work:

1. **Accounts and teams** — more than one person can see agent time.
2. **A product** — install once, never think about it again, and a second
   machine that signs in just works.

They are the same thing because the second one is blocked on the first.
Today, "share this machine with my other machine" means provisioning a
PostgreSQL database yourself and exporting `CC_INSIGHTS_SYNC_URL`. An
account replaces that with a sign-in, and teams then reuse the identity,
the transport and the scoping that sign-in requires. Build the service
once.

## 0. What already exists, and what does not

Worth being precise, because the groundwork is unusually good here and it is
easy to over- or under-estimate what is left.

**Exists.** `host_id` on every row. Every id a content hash, so the same
logical row computes the same id on any machine and a row that crosses twice
collapses instead of duplicating. `cci sync push | pull | status` over a
shared PostgreSQL, measured at 191,475 rows pushed in 5.5 s. A redaction
layer that classifies all 110 schema columns closed-by-default, and
`redact.publication(conn, actor)`, which already takes `actor` as a parameter
against exactly this day.

**Does not exist.** Any server. `serve.py` is read-only localhost; `sync.py`
talks straight to a Postgres the user owns. There is no identity, no token,
no tenant column, and **no publish transport at all** — `redact` builds the
projection and `cci privacy` prints it, and nothing sends it anywhere.

## 1. Four decisions

Taken 2026-09-20. Recorded so they are not relitigated inside a work package.

| | Decision | Why |
| --- | --- | --- |
| **Hosting** | One private instance, for one team | Not SaaS. No public signup, no billing, no abuse surface. The schema is tenant-aware regardless, so growing into SaaS later is a deployment change rather than a rewrite. |
| **Storage** | Two stores: full personal + redacted team | Below. This is the load-bearing one. |
| **Auth** | GitHub OAuth now, email later | Groups already carry `forge`/`owner`/`repo`, so team scope is *derived* from repo access instead of hand-maintained. Email is a second identity for people whose repos are not on GitHub, and it is additive. |
| **Install** | PyPI plus a thin `curl \| sh` | `pipx install cc-insights` is the substrate; the one-liner wraps Python, the venv, sign-in and the background job. |

## 2. Two stores, because there are already two pipes

This is the decision everything else follows from, so it gets the space.

`sync.py` and `redact.py` are not two versions of the same thing. They move
different data under different rules, and the module docstrings say so
explicitly — `sync.py` ends with *"do not widen this one to reach them"*.

- **`sync`** moves everything between **one person's machines**, filesystem
  paths included, because it is all the same person's disk.
- **`redact`** builds what a **second person** may see: re-keyed on the git
  remote, no paths, no hostnames, no `project_id` — because
  `project_id = sha256(root_path)` and 555 guesses recovered 20% of the
  author's corpus from the ids alone.

An account is where those two meet, and the temptation is to collapse them:
one store with full rows, filtered on read. **That is rejected**, and not on
taste. Filtering at query time fails the first time anything goes wrong —
one API bug, one backup, one `psql` session — and there is no un-leaking. It
is the same discipline that has kept prompt text out of the schema for the
whole project: enforced where the row is *written*, not where it is read.

So:

```
  laptop  ─── personal transport ──▶  personal store   (full rows, paths,
    │                                                   one account only)
    └────── publish transport ─────▶  team store       (redact.publication(),
                                                        no path ever arrives)
```

Both are pushed **from the laptop**, and the projection is computed there.
The team store never receives a path, so there is nothing to leak from it.

The cost of this choice, stated plainly: two transports to build and two
stores to operate, and the same span is stored twice in different shapes.
At ~191 k rows that is cheap. The alternative saves that and gives up the
one property that makes the whole thing safe to run.

### What the personal store is *not*

It is not a shared team database with a filter on it. It holds
`~/Coding/<client-name>`, hostnames, and every `cwd` the agents ran in. If a
second person can read it under any circumstance, the redaction layer was
pointless. Single-account readable, no exceptions, and no "admin can see
everything" mode — an admin who can read it is a second person.

## 3. The transport question, which is a real fork

`cci sync` currently connects to PostgreSQL directly with psycopg. With an
account, the client cannot keep raw database credentials: those read
everyone's rows.

**Option A — keep direct Postgres, add row-level security.** A role per
account, RLS policies on every table. `sync.py` barely changes, and the
measured 5.5 s push is preserved for free.

**Option B — an HTTP transport with a bearer token.** `sync.py` grows a
second backend; the batched upserts become a wire protocol.

**Decision: B**, implemented. Three reasons, in order of weight:

1. **An RLS misconfiguration is a silent total leak.** One policy missing
   from one table, and every account reads every other account's paths.
   Nothing surfaces it — the queries keep working. This store holds the most
   sensitive data in the system.
2. **The roadmap already requires server-side logic that a database cannot
   express.** Aggregates must be computed inside the viewer's scope (§5), and
   branch names need a per-repo opt-out. Those are application rules.
3. **Exposing PostgreSQL to the public internet** is a worse operational
   posture than exposing one HTTPS endpoint, and every machine that signs in
   is on a different network.

B's cost is real and should be budgeted: the push protocol has to batch,
resume and stay idempotent across ~191 k rows. Idempotency is free — every
id is already a content hash — so this is mostly framing and pagination, not
new semantics. Keep direct-Postgres `cci sync` working for anyone who wants
to run their own database; it is a supported mode, not dead code.

## 4. Schema

Four new tables, one new column. All ids TEXT, all timestamps epoch-ms, same
portability contract as `migrations/001_init.sql`.

```sql
CREATE TABLE account (          -- a person
    account_id  TEXT PRIMARY KEY,
    created_at  INTEGER NOT NULL
);

CREATE TABLE identity (         -- how they prove it; more than one per account
    identity_id TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    provider    TEXT NOT NULL,          -- 'github' | 'email'
    subject     TEXT NOT NULL,          -- the provider's stable user id
    UNIQUE (provider, subject)
);

CREATE TABLE team (
    team_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE team_member (
    team_id     TEXT NOT NULL REFERENCES team(team_id),
    account_id  TEXT NOT NULL REFERENCES account(account_id),
    role        TEXT NOT NULL,          -- 'member' | 'admin'
    PRIMARY KEY (team_id, account_id)
);

ALTER TABLE host ADD COLUMN account_id TEXT REFERENCES account(account_id);
```

`host_id` does **not** change and does not become an account id. It is baked
into every session id, and regenerating or reassigning it forks the entire
history — `config.py` has a comment and an atomic write guarding exactly
that. A host is *claimed by* an account; it keeps its identity.

`identity` is a separate table rather than columns on `account` because the
decision is "GitHub now, email later". One account, two ways to sign in, no
migration when the second arrives.

### Four things this section got wrong

Found while building the server against it. Recorded rather than quietly
corrected, because each one is a hole somebody could reopen.

1. **`ALTER TABLE host ADD COLUMN account_id` is not sufficient for the
   server.** It is right for the *client*, where there is one account. On
   the server, `project_id = sha256(root_path)` — so two CI boxes that both
   build in `/home/ci/work` compute the **same id for different accounts**.
   With the client's single-column primary key those collapse into one row,
   and the `UNIQUE` on `root_path` turns a coincidence into an error that
   tells one account something true about another's disk. Every key and
   every foreign key in the personal store is therefore composite on
   `account_id`.

2. **Nothing said who may add a repo to a team roster,** and "any admin, any
   `repo_id`" is a hole straight through the boundary. REDACTION.md §0 says
   publishing `repo_id` is safe *because* the remote is public — which is
   exactly what makes the id computable by anyone who can guess the remote.
   An admin may only roster a repo already in their own scope.

3. **§1 and §6 contradicted each other.** Scope is derived from repo access,
   but the client never holds a GitHub token — leaving nothing in the system
   able to ask GitHub the question. Resolved by listing accessible repos
   during the one request where the token exists, then discarding it. The
   cost is real and is stated in the contract: **repo access is only as
   fresh as the last `cci login`.**

4. **`account` had no name, but `redact.Session.actor` must be
   attributable.** Added `account.actor`, stamped once and never following a
   GitHub rename — a display name that moves would silently re-attribute
   history. A batch whose actor does not match is a 403, not an overwrite.

Every table in the personal store gains `account_id` and every query filters
on it. `redact.FIELDS` must classify the new columns, or its
closed-by-default test fails the build — which is the intended behaviour and
how it caught v1's three cost tables during the v2 merge.

## 5. Two rules the API must enforce from its first commit

Lifted from `docs/ROADMAP.md` because they are easy to lose and expensive to
retrofit.

**Aggregates are computed inside the viewer's scope.** A precomputed
"Alice: 40 h this week" that spans a repo Bob cannot see leaks that repo's
*existence* the moment Bob reads the total. This forbids the obvious
optimisation — a nightly rollup table — unless the rollup is keyed by
(viewer scope, period), and it is why scoping cannot be a `WHERE` clause
bolted onto existing queries later.

**Branch names get a per-repo opt-out.** They are publishable under the repo
rule — anyone with repo access can run `git branch -r` — but they are free
text, and `feat/restricted-org-dbs` can say more than its author meant.
Default on, one switch per repo.

And one rule this document adds:

**Withheld time stays counted.** `redact` already reports the hours that
cannot be published (8% of the author's corpus, the work with no git
remote), and the team view must show that number rather than quietly
omitting it. A dashboard that silently drops part of someone's week is not
private, it is wrong, and the person reading it cannot tell the difference.

## 6. Signing in from a second machine

The flow the product ask is really about:

```bash
pipx install cc-insights
cci login                 # device-code flow; prints a code, opens GitHub
cci install               # claims this host, starts the background job
```

`cci login` uses the **OAuth device authorisation grant**, not a redirect: a
CLI cannot reliably receive a browser callback, and a local listener on a
random port is the fragile version of this. GitHub supports it, and it works
identically over SSH, which matters for the machine that is not the one in
front of you.

The token is stored in the config directory, `0600`, and **not** in
`config.toml` — that file is plain text that people copy around, which
`config.py`'s `_db_path_for_toml` exists because someone already did.

After that, `cci install` claims the host for the account and the existing
background job gains a push step. Nothing about ingest, derive, the
dashboard or the metrics layer changes: local SQLite stays the source of
truth, exactly as it does for `cci sync` today. That is the property that
made multi-machine a backend swap rather than a migration, and it is worth
preserving deliberately.

## 7. Deliberately not in the first version

- **Public signup, billing, plan limits.** One private instance was the
  decision.
- **Pointing the dashboard at the server.** `db.to_dialect` is deliberately
  naive — it covers the DDL and the sync statements and is not a general
  query translator. The read path stays SQLite.
- **Publishing cost.** Excluded from sync by design and classified closed for
  publication. A per-repo cost aggregate is a reasonable thing for a team to
  want and there is no field for it; it needs its own decision, not a
  default.
- **Anything derived from team data about individuals.** Not a technical
  limit. This data can answer "who worked the most hours", the answer will
  be wrong (it measures agent time, not work), and it will be quoted anyway
  — the same reason `docs/ROADMAP.md` refuses to ship "time saved".
