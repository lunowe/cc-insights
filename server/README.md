# CC-Insights account server

The service behind `cci login`, `cci sync` over HTTP, and the team view.
Its wire contract is [`docs/SERVER_API.md`](../docs/SERVER_API.md) and that
document is frozen; its design is [`docs/ACCOUNTS.md`](../docs/ACCOUNTS.md)
and the privacy rules it enforces are
[`docs/REDACTION.md`](../docs/REDACTION.md).

**A separate package.** The root `cc-insights` installs with zero
dependencies, and its pyproject says that is "a feature, not an accident".
FastAPI, uvicorn and psycopg are three dependencies nobody running `cci
ingest` needs. Nothing here is imported by the client.

## Two stores

| | holds | readable by |
| --- | --- | --- |
| personal | full rows, paths, hostnames, `cwd` | exactly one account |
| team | `redact.publication()` — repos, sessions, spans | anyone who can see the repo |

They are separate stores rather than one store with a filter on read, and
`docs/ACCOUNTS.md` §2 is the argument. The short version: filtering at query
time fails the first time anything goes wrong — one API bug, one backup, one
`psql` session — and there is no un-leaking.

**The team store has no path column.** Not nullable and unused: absent.
`migrations/003_team_store.sql` is the enforcement and
`tests/test_schema.py::test_team_store_has_no_path_column` reads the live
catalog to keep it that way.

## Running it

```bash
export CCI_SERVER_DATABASE_URL=postgresql:///cci_server
export CCI_SERVER_GITHUB_CLIENT_ID=...       # an OAuth app with device flow on
export CCI_SERVER_GITHUB_CLIENT_SECRET=...
export CCI_SERVER_GITHUB_SCOPE='read:user repo'   # `repo` enables §4.6

cci-server migrate     # apply pending migrations and stop
cci-server run         # 127.0.0.1:8788 by default
```

It binds to localhost unless told otherwise. A server holding other people's
filesystem paths should make somebody type the address it listens on.

With no GitHub app configured it still runs — every account that already has a
token keeps working, and sign-in returns `502 upstream_unavailable`. A
misconfigured secret should not be a total outage.

## Tests

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest
```

They run against a **real PostgreSQL**, because half of what this server
enforces is enforced by the schema: composite keys that make a cross-account
join unrepresentable, a CHECK on `thread_role`, a deferred self-reference on
`thread.parent_thread_id`, and the absence of any path column. A stub would
pass those tests while proving nothing.

The suite creates a throwaway database on `postgresql:///postgres` (override
with `CCI_SERVER_TEST_ADMIN_URL`) and drops it afterwards. With no PostgreSQL
reachable it **skips** rather than falling back to something weaker: a green
run against a substitute is a worse outcome than a skipped one, because only
one of the two is honest about what was checked.

Two tests additionally import `cc_insights` from `../src` to prove that
`personal_schema.py` has not drifted from `sync.TABLES` and that
`repoid.repo_id` agrees with `redact.repo_id`. They skip when the client
source is not beside the server.
