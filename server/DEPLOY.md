# Deploying the account server

> **Deployed 2026-09-20.** Railway project `cc-insights` in workspace
> "Personal Projects": service `server` (Dockerfile, rootDirectory `server`)
> and service `Postgres` (managed, 5 GB volume).
>
> **https://YOUR-SERVER.up.railway.app**
>
> Migrations 1-4 applied at first boot. `GET /v1/personal/tables` returns
> `401 unauthenticated`, which is the server working. **Sign-in does not work
> yet**: no GitHub OAuth app is configured, so `POST /v1/auth/device/start`
> returns `502 upstream_unavailable` naming the two missing variables. See
> "Prerequisites" below — that is the one remaining step, and it needs a
> browser.

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

Two things cost an hour the first time and are worth writing down.

**`railway up` deploys the directory you run it in, and `rootDirectory` is
applied inside that upload.** Running it from `server/` uploads the server
directory and then looks for `server/` *inside* it — `lstat .../snapshot-
target-unpack/server: no such file or directory`. Run it from the repo root
with `rootDirectory=server`, or from `server/` with no root directory. Not
both.

**Link before you deploy.** `railway up` in an unlinked directory does not
fail — it creates a brand-new project and deploys there, which is a second
project with a half-built service in it and no warning that this is not what
you meant.

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

- **Joining a team takes the joiner's own credential.** This used to say that
  `add_member` had no consent — any admin could add any account by id. That
  route is **gone**. An admin now mints a join code (`cci team invite`) and
  sends it however they like; the colleague redeems it with their own token
  (`cci team join <code>`), and that redemption is the consent. Both ends are
  recorded, so `cci team members` answers "who let this person in" and
  `cci team invites` answers "who did I let in, and when".

  What this means for you operationally:

  - **A join code is a password.** It grants a stranger membership of a team
    that reads other people's agent time. Send it the way you would send a
    password, and prefer a direct message to a channel.
  - **It is shown exactly once,** at `cci team invite`. It is stored hashed
    and there is no command, and no database query, that can show it again.
    If somebody loses it, mint another and revoke the first.
  - **Defaults are single-use and 72 hours.** Override with `--uses` and
    `--expires-in` when you are onboarding several people at once, and prefer
    several single-use codes to one shared code if you want the audit trail
    to say who is who.
  - **Revoke, do not just remove.** `cci team remove <who>` takes somebody
    off the roster, and a *spent* single-use code cannot readmit them. A code
    with seats left can. Run `cci team invites` after any removal you meant.
  - **An invalid code and an expired one look identical** to whoever tried
    it, on purpose. When a colleague says "it says the code is not valid",
    the server will not tell you which; check `cci team invites` yourself.
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
