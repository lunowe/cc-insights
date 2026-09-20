# Deploying the account server

> **Nothing here has been run.** The Dockerfile builds a wheel that has been
> verified to carry its four migrations, but the image itself has not been
> built (no Docker daemon on the machine this was written on) and no instance
> exists. Treat every command below as a plan, not a transcript.

## What you are standing up, and what it holds

Two stores in one PostgreSQL database, with different rules:

| store | holds | readable by |
| --- | --- | --- |
| personal | full rows: `root_path`, `cwd`, hostnames | exactly one account |
| team | the redacted projection; **no path column exists** | people who can already see the repo |

The personal store is the sensitive one. It contains `~/Coding/<client-name>`
for every project you have ever run an agent in. `docs/ACCOUNTS.md` §2 says
plainly that if a second person can read it under any circumstance, the whole
redaction layer was pointless — and that there is deliberately no "admin can
see everything" mode, because an admin who can read it is a second person.

Decide whether you are comfortable with that living on a hosted platform
before you run any of this. It is a legitimate reason to keep the server on
a machine you own.

## Prerequisites

1. **A PostgreSQL database.** Managed is fine. Nothing else is needed — no
   Redis, no object store.
2. **A GitHub OAuth app with device flow enabled.** Settings → Developer
   settings → OAuth Apps → New. Tick *Enable Device Flow*; there is no
   callback URL to set, because the device grant does not use one.
   - Scope `read:user` is enough to sign in.
   - Scope `repo` is what makes forge-derived access work
     (`docs/SERVER_API.md` §4.6). It is a broad grant to ask of everyone, and
     an instance running on team rosters alone works without it — but note
     the consequence: **without it, nobody is ever forge-verified, so a
     roster only ever shares the adder's own rows.** That is fail-closed and
     correct, and it is also probably not what you want for a real team.

## Environment

```
CCI_SERVER_DATABASE_URL=postgresql://...      # required; no default, on purpose
CCI_SERVER_GITHUB_CLIENT_ID=...
CCI_SERVER_GITHUB_CLIENT_SECRET=...
CCI_SERVER_GITHUB_SCOPE='read:user repo'      # drop `repo` to opt out of §4.6
PORT=8788                                     # the platform usually sets this
```

There is no config file and no default database URL. `config.py` says why: a
default would be a database somebody did not choose, and this one holds
filesystem paths.

## On Railway

```bash
railway init
railway add --database postgres
railway variables set CCI_SERVER_GITHUB_CLIENT_ID=... \
                      CCI_SERVER_GITHUB_CLIENT_SECRET=... \
                      CCI_SERVER_GITHUB_SCOPE='read:user repo'
railway up                  # builds server/Dockerfile
railway domain              # a public hostname
```

Point `CCI_SERVER_DATABASE_URL` at the provisioned Postgres — on Railway that
is the `DATABASE_URL` the plugin exports; reference it rather than pasting the
value, so a credential rotation does not silently strand the app.

Migrations run at container start (`cci-server migrate && cci-server run`),
deliberately: a release that adds one must not leave the server refusing every
request with the reason in a log nobody reads. Same argument as the client's
job leading with `cci init`, and the same idempotence makes it free.

## Pointing a machine at it

```bash
cci login --server https://<your-host>
cci install          # claims this host and starts the background job
cci doctor           # confirms sign-in, last push, and that the token works
```

## Before you let a second person on

These are the things that are *not* the code's call, and the code cannot make
them for you:

- **`add_member` has no consent.** Any team admin can add any account by id.
  It grants the admin nothing today — a roster shares the *adder's* access,
  not the member's — but it means somebody can be put in a team without being
  asked, and it becomes load-bearing under any future membership-based rule.
- **Anyone can publish their own rows under any `repo_id`.** Not a disclosure,
  but a stranger who guesses your remote can inject rows under their own actor
  name into a repo your team reads. There is no write-side rule for this yet.
- **Branch names are published by default.** `docs/ACCOUNTS.md` §5 rule 2.
  They are free text, and `feat/restricted-org-dbs` can say more than its
  author meant. Switch them off per repo before inviting people, not after.
- **Withheld time is reported, not hidden.** Work with no git remote cannot be
  published — 8% of the author's corpus — and the team view shows the hours it
  is not showing. Expect that number and do not treat it as a bug.

## Operational notes

- The server binds `127.0.0.1` by default and the image overrides it to
  `0.0.0.0`, because a container's localhost is reachable by nothing. The
  default stays private so a laptop-run server is private by accident rather
  than by remembering.
- **Back up the database before every upgrade that adds a migration.** They
  are additive and applied in one transaction each, but the personal store is
  the only copy of nothing — every row can be recomputed from the client's
  SQLite — while the *team* store is the only copy of the projection.
- Tokens do not expire. Revocation exists; there is no rotation story yet.
- No rate limiting on `POST /v1/auth/device/start`, and nothing garbage-collects
  `device_authorization`. On a private instance behind a known audience that is
  tolerable. It would not be on a public one.
