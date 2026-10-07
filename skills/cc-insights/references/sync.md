# Several machines

Each machine ingests its own logs locally, pushes the rows it owns, and pulls
the other machines' rows back. Local SQLite stays the source of truth. Every
id is a content hash, so re-running any sync command is safe: rows that cross
twice collapse into one, and an interrupted push resumes where it stopped.

Sync is for **one person's** machines. It carries full rows, including paths
and hostnames, into a store only that account can read. Sharing with other
people goes through `cci publish` ([teams.md](teams.md)), which applies
different rules.

## 1. Get a server URL

Signing in needs an account server, and there is **no public default**. A
join link from a team admin (`https://<server>/join/<code>`) carries the
server: `cci team join <link>` signs in and joins in one step (teams.md).
Otherwise ask the user for their server URL. On a machine with nothing
installed yet, `install.sh --server <url>` installs and signs in. Without a
server, there are two options:

- **Self-host one:** PostgreSQL plus a GitHub OAuth app with device flow
  enabled. The full guide is `server/DEPLOY.md` in the repo
  (https://github.com/lunowe/cc-insights/blob/master/server/DEPLOY.md).
- **Skip the server:** use the user's own PostgreSQL directly. See
  "Direct PostgreSQL" below.

The URL is resolved in this order: `--server`, then `$CC_INSIGHTS_SERVER`,
then `server_url` in `config.toml`, then the saved credential. After the
first sign-in, no command needs the URL again.

## 2. Sign in

`cci login` is a device-code flow. It prints a URL and a code, then **blocks**
until the user approves the code in a browser that is signed in to GitHub.
Your shell output is probably not visible to the user while the command
waits, so run it in the background and relay the code:

```bash
nohup cci login --server "$URL" > /tmp/cci-login.log 2>&1 &
# a few seconds later, in a separate call:
cat /tmp/cci-login.log
```

Give the user the `Open this page` URL and the `Enter this code` value (or
the `go straight to` link). Then poll the log every few seconds, each check
in its own call, until `grep -q 'signed in as' /tmp/cci-login.log`
succeeds. If the log ends in an error instead, for example an expired code,
start the login again. Done when the log shows `signed in as <name>`. Alternatively,
the user can run it in their own terminal. In Claude Code they can type
`! cci login --server <url>`.

`cci team join <link>` and the installer's `--join`/`--server` run this
same sign-in; relay their codes the same way.

The device flow works over SSH, so a remote machine can be signed in from
the laptop's browser. Signing in claims this host for the account and stores
the token in `credentials.toml` (mode 0600). `cci logout` clears the token
and revokes it on the server.

## 3. Move data

```bash
cci sync push      # send this machine's rows
cci sync pull      # bring the other machines' rows down
cci sync status    # what the server holds, per host
```

**The background job pushes to the account, and never pulls.** The job
runs `cci sync auto` after each ingest. That pushes when the machine is
signed in, and does nothing otherwise. Run `cci sync pull` whenever the user wants the other
machines' history in this machine's dashboard.

For a second machine: install there (SKILL.md, steps 1–3), then
`cci login --server <url>`. `cci doctor` then also reports `account` and
`last push`, plus `auto-push` on macOS.

## Direct PostgreSQL

This mode is still fully supported: there is no account server, and each
machine talks to one shared database. The driver is an optional extra,
installed into the same environment `cci` runs from:

```bash
pipx inject cc-insights 'psycopg[binary]>=3.1'      # pipx install
<venv>/bin/pip install 'cc-insights[postgres]'      # venv or checkout install
export CC_INSIGHTS_SYNC_URL=postgresql://user@host/cci   # or sync_url in config.toml
cci sync push --direct
```

For manual `cci sync` commands, a configured `sync_url` wins over being
signed in. `--account` and `--direct` force one or the other, and every
command prints which one it used. **The background job never pushes in
direct mode:** `sync auto` and the watcher push only to an account server.
In direct mode, either run `cci sync push` when needed, or add
`cci sync push --direct` to a scheduled job yourself. Anyone who can read
that database can read every path in it, so it should be the user's
alone.
