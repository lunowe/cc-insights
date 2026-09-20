# CC-Insights account server — frozen contract

The HTTP surface between `cci` on a laptop and the account server. Client and
server are built in parallel against this document; **it is frozen** — if it
needs to change, say so rather than diverging.

> **Amended 2026-09-20, saying so rather than diverging.** A security review
> found that the scope rule in §4 was not the rule the server should have
> been enforcing: it treated a self-asserted `repo_id` as evidence of repo
> access, so two HTTP calls read any repo's data. §4, §4.1, §4.2, §4.5 and
> §4.6 now describe the rule that is actually enforced, and the change is a
> narrowing — nothing that was refused before is permitted now. Separately,
> §4.4 and §4.8 were **wrong rather than changed**: the contract said every
> aggregate carries a `withheld` block and `/daily`'s own example showed it
> without one. The example was corrected to match the stated rule, and the
> server was corrected to match both.

> **Amended again, saying so rather than diverging.** `POST
> /v1/teams/{teamId}/members` is **removed** and replaced by join codes —
> §4.5.1, and the register in §4.5. This is the one amendment so far that
> takes something away, so it is worth being plain about: an integration
> calling the old route gets `405`, deliberately and not `404`, because the
> path still serves `GET`. The reasons are recorded in `docs/ACCOUNTS.md` §5a
> and were flagged as unresolved in `server/DEPLOY.md` before this change;
> the short version is that adding somebody by id had no consent behind it
> and needed an account id nothing in the system would tell you. Two
> endpoints gained fields rather than changing: `GET …/members` now carries
> `joinedAt` and `invitedByActor`, and `DELETE …/members/{accountId}` now
> also accepts the caller removing themselves.

`docs/API.md` is the other frozen contract in this repo and describes a
different thing: `cci serve`, read-only, localhost, no auth. This one is the
opposite on all three counts, so nothing here is shared with it except the
conventions in the next section.

Prerequisites, in the order they must be read: `docs/REDACTION.md` states the
boundary rule this server exists to enforce, and `docs/ACCOUNTS.md` records the
four decisions — one private instance, two stores, GitHub device flow, and the
HTTP transport this document specifies.

## 0. Conventions

- Base path is `/v1`. The version is in the path because a CLI installed a year
  ago keeps talking to a server deployed last night, and the two have to be
  able to disagree about the answer without disagreeing about the question.
- All times on the wire are **epoch milliseconds UTC**, as everywhere else in
  this project. Durations are milliseconds, named `*Ms`.
- JSON in, JSON out. `Content-Type: application/json`.
- Field names are `camelCase` on the wire and `snake_case` in the database, the
  same split `docs/API.md` already uses. The **one exception is bulk row
  transfer**: `/v1/personal/push` and `/v1/personal/pull` name columns exactly
  as the schema does, because those payloads are the schema and renaming them
  twice per round trip would be a translation layer with no reader.
- Every response that is not 2xx has exactly this body and no other:

  ```json
  { "error": "machine_code", "message": "A sentence for a human." }
  ```

  `error` is a stable identifier a client may branch on; `message` is prose and
  may change. Never a 200 with an error in it.
- **Metadata only, forever.** No endpoint here accepts or returns prompt text,
  response text, tool arguments or file contents. That is a release blocker in
  this project, not a style rule, and it is the reason the bulk endpoints name
  their columns explicitly instead of accepting an object.

### Authentication

Every endpoint except `GET /healthz` and the two device-flow endpoints requires

```
Authorization: Bearer ccis_<43 url-safe base64 characters>
```

Missing, malformed, unknown, expired or revoked token: `401` with
`{"error": "unauthenticated"}`. An authenticated caller who may not do the
thing: `403` with a more specific code. The distinction matters because a CLI
must re-run `cci login` for the first and must not for the second.

**A 403 is never used to tell you that something exists.** Asking for a repo
outside your scope, a team you are not in, or another account's rows returns
`404 not_found` — the same answer as asking for something that does not exist
at all. That is `docs/ACCOUNTS.md` §5 rule 1 applied to status codes: a
distinguishable 403 would leak the existence of the row it refused.

### Pagination

Every endpoint that can return an unbounded number of rows is paginated the
same way, and it is **keyset, never offset**:

```
GET ...?limit=1000&cursor=<opaque>
→ { "rows": [...], "nextCursor": "<opaque>" | null }
```

- `cursor` is opaque. It encodes the last key returned. Clients must pass it
  back verbatim and must not parse it.
- `nextCursor: null` means the page you just received was the last one.
- A cursor stays valid across server restarts and across rows being inserted
  behind it, which is the property `OFFSET` does not have. On a corpus measured
  at 191,475 rows, an `OFFSET 190000` page also re-scans 190,000 rows to throw
  them away.
- `limit` defaults to 1000 and is capped at 5000. A request above the cap is
  not an error; it is silently clamped, and the response says what was used.

## 1. `GET /healthz`

Unauthenticated. `200 {"status":"ok","migrations":[1,2,3],"now":1789913704558}`.
Returns `503` with `{"error":"database_unavailable"}` when the database is not
reachable, so a load balancer removes the instance rather than serving 500s.

## 2. Sign-in — the OAuth device authorisation grant

Per `docs/ACCOUNTS.md` §6. A redirect flow is not an option: a CLI cannot
reliably receive a browser callback, and a local listener on a random port is
the fragile version of that. The device grant also works identically over SSH,
which is the case that matters — the second machine is usually not the one in
front of you.

**The client never sees a GitHub token.** The server performs the GitHub half
of the exchange, and hands back a token of its own. A GitHub token is a
credential for every repo the person can reach; the token this server issues
reads one account's agent-time metadata. Handing the stronger one to a CLI that
writes it to a file on disk would be a strictly worse trade than the one this
flow exists to make.

### `POST /v1/auth/device/start`

Unauthenticated. Body is optional and carries only a label for the token that
will eventually be minted:

```json
{ "clientName": "cci 0.1.0 on studio" }
```

`200`:

```json
{
  "deviceCode": "dev_9rXk…",
  "userCode": "WDJB-MJHT",
  "verificationUri": "https://github.com/login/device",
  "verificationUriComplete": "https://github.com/login/device?user_code=WDJB-MJHT",
  "expiresAt": 1789914604558,
  "interval": 5
}
```

- `deviceCode` is **this server's** device code, not GitHub's. GitHub's stays on
  the server. It is a bearer-equivalent secret for the duration of the flow and
  is stored hashed, like every other token here.
- `userCode` is what the person types into `verificationUri`. Print both.
- `interval` is the **minimum** seconds between polls. It can go up; see below.
- `expiresAt` is when the flow dies. Typically ~15 minutes.

`502 {"error":"upstream_unavailable"}` if GitHub cannot be reached. This is
worth distinguishing from a 500: it is not the client's problem and it is
usually transient.

### `POST /v1/auth/device/token`

Unauthenticated. The poll.

```json
{ "deviceCode": "dev_9rXk…" }
```

**`200` — authorised:**

```json
{
  "accessToken": "ccis_…",
  "tokenType": "bearer",
  "accountId": "acc_…",
  "actor": "lunowe",
  "expiresAt": null
}
```

`accessToken` is shown **once** and never again; the server keeps only its
hash. `expiresAt: null` means the token does not expire on a clock — it ends
when it is revoked. `actor` is the name this account's published rows will
carry, and the client should display it so a person can see which identity they
just signed in as before any data moves.

**Everything else is `400` with the standard error body**, following RFC 8628
§3.5 so the codes are the ones an OAuth implementer already knows:

| `error` | Meaning | Client must |
| --- | --- | --- |
| `authorization_pending` | Nobody has approved it yet | Wait `interval`, poll again |
| `slow_down` | You are polling too fast | **Raise its interval** to the returned `interval`, then poll again |
| `expired_token` | The flow timed out | Stop. Start a new one. |
| `access_denied` | The person clicked cancel | Stop. Do not retry. |
| `invalid_device_code` | Unknown, or already exchanged | Stop. Start a new one. |

One code is **not** a 400: `502 upstream_unavailable`, for GitHub being
unreachable or answering with something outside the five above. It is not the
client's problem and it is usually over in a minute, so it carries a
`Retry-After` and is retryable — but an unrecognised GitHub error is never
translated into `authorization_pending`, because a client that retries forever
on an unknown error never tells its user what went wrong.

The two retryable codes carry the extra fields the client needs:

```json
{ "error": "slow_down", "message": "…", "interval": 10, "retryAfter": 10 }
```

and set a `Retry-After: 10` header. The two terminal codes carry neither, which
is how a client can tell them apart without a table.

**`slow_down` has two sources and both must be honoured.** GitHub returns it
when the server polls too fast, and this server returns it when the *client*
polls faster than the interval it was given — without forwarding that poll to
GitHub at all. The second exists because one impatient CLI can get the whole
instance rate-limited at GitHub, and then nobody can sign in. The interval only
ever increases within a flow, by 5 seconds each time, and the current value is
in every `authorization_pending` response too, so a client that only reads
`interval` and ignores the error code still converges.

### `GET /v1/auth/whoami`

```json
{
  "accountId": "acc_…",
  "actor": "lunowe",
  "createdAt": 1789900000000,
  "identities": [
    { "provider": "github", "subject": "1234567", "label": "lunowe",
      "createdAt": 1789900000000 }
  ],
  "teams": [ { "teamId": "tm_…", "name": "Platform", "role": "admin" } ],
  "token": { "tokenId": "tok_…", "name": "cci 0.1.0 on studio",
             "createdAt": 1789900000000, "lastUsedAt": 1789913704558,
             "expiresAt": null }
}
```

`identities` is an array, and it is an array today when only GitHub ships,
because that is the whole reason `identity` is a table rather than two columns
on `account` (`docs/ACCOUNTS.md` §4). A client that renders it as a list needs
no change when email login arrives.

### `GET /v1/auth/tokens`

Every live token on this account: `{"tokens": [ …the `token` shape above… ]}`.
Never the token itself, and never its hash — a hash of a 256-bit random string
is not guessable, but publishing it makes an offline check possible against a
stolen backup, and there is no reason to.

Revoked tokens are included for 30 days with `revokedAt` set, then disappear.
Somebody checking whether a lost laptop's token is dead needs to see that it is
dead, not to see nothing and wonder whether they clicked the button.

### `DELETE /v1/auth/tokens/{tokenId}`

`204`. Idempotent — revoking an already-revoked token is a `204`, not a 404,
because the caller's goal is already true. `404 not_found` for a token on
another account (not 403; see §0).

### `POST /v1/auth/logout`

`204`. Revokes the token presenting the request. Exists separately because it
is the one revocation a client can perform without knowing its own `tokenId`,
and making `cci logout` do a round trip to learn its own id first would be one
more thing to fail.

## 3. The personal store

One person's own rows, including filesystem paths, readable by **exactly one
account**. `docs/ACCOUNTS.md` §2 is unambiguous about what this is not: not a
shared database with a filter on it, no admin-can-see-everything mode, because
an admin who can read it is a second person.

This is the wire form of what `cc_insights.sync` already does directly against
PostgreSQL. The ownership rules, the conflict clauses and the excluded tables
are `sync.TABLES` and `sync.EXCLUDED` unchanged — the transport moved, the
semantics did not.

### Tables and columns

| table | key | pushed rows |
| --- | --- | --- |
| `host` | `host_id` | all of this account's |
| `project_group` | `group_id` | all |
| `project` | `project_id` | all |
| `project_probe` | `project_id, host_id` | this host's |
| `session` | `id` | this host's |
| `thread` | `id` | this host's sessions' |
| `event` | `id` | this host's sessions' |
| `span` | `id` | this host's sessions' |

`GET /v1/personal/tables` returns this table as data — name, key columns, full
column list in order, and whether the table is host-scoped — so a client can
check for drift instead of hardcoding a copy that silently rots. A column the
server does not know is a `400 unknown_column`, never a silently dropped value.

`ingest_file`, `schema_migrations`, `model_price`, `event_cost` and
`event_unpriced` are **not** transferred, for the reasons in `sync.EXCLUDED`.
Pushing one is `400 table_not_transferred` with the reason in `message`.

### `POST /v1/personal/push`

One table per request.

```json
{
  "hostId": "h_…",
  "table": "span",
  "columns": ["id", "session_id", "thread_id", "started_at", "ended_at",
              "event_count", "attended"],
  "rows": [["sp_…", "se_…", "th_…", 1789900000000, 1789900060000, 12, 1]]
}
```

`200`:

```json
{ "table": "span", "received": 1, "applied": 1, "rejected": 0, "conflicts": [] }
```

- `columns` must contain **every required column**, in any order. A subset is a
  `400 missing_column`: a partial row would upsert NULLs over data that is
  already there. A name the server does not know is `400 unknown_column`,
  never a silently dropped value.
- **Optional columns** may be present or absent, and an absent one is left
  untouched rather than nulled. There is exactly one today:
  `event.cache_write_1h_tokens`, which migration 005 of the client schema
  added and `sync.TABLES` was never extended for. So a client on the current
  release omits it and a newer one sends it, and neither erases the other's
  work — which matters, because on the author's corpus 41% of cache-write
  tokens bought a one-hour TTL and nulling the column reprices them as
  five-minute writes.
- Each cell must be a string, a number or null. An object or an array is
  `400 invalid_value`, and a string past 4096 characters is
  `400 value_too_long`. Nothing in this schema is prose; both refusals are the
  metadata-only rule enforced where the row is written rather than hoped for.
- `rows` is positional, matching `columns`. Max **5000 rows** per request;
  above that is `413 batch_too_large` with the cap in `message`. The measured
  corpus is 191,475 rows, so this is ~40 requests for the largest table.
- `hostId` is required and must be a host this account has pushed or is pushing
  in this request. A host belonging to another account is `404 not_found`.
- **Idempotent.** Every id in this schema is a content hash, so re-pushing a
  batch changes nothing and reports the same numbers. `applied` counts rows
  sent and accepted, not rows that changed — knowing what actually changed
  would mean reading every row back, which costs more than the push.
- One request is one transaction. A batch either lands whole or not at all.

**Order matters, and the client must send it.** `sync.TABLES` is in foreign-key
order and `sync.order_by_parent` already puts parent threads before their
children; batching preserves both. A row whose parent has not arrived is
`409 foreign_key_violation`, naming the missing table in `message`, rather than
a 500. Within a single request the ordering is not the client's problem — the
self-reference on `thread.parent_thread_id` is deferred to commit.

**Conflicts.** `conflicts` lists ids the server refused to overwrite because
they belong to a different account. In the personal store it is *structurally*
always empty — the primary key is `(account_id, <id>)`, so the same id under
two accounts is two legitimate rows and there is nothing to refuse. It is in
the response anyway so that a client has one shape across both bulk endpoints;
`POST /v1/team/publish`, where ids are global, genuinely populates the
equivalent `rejected` count.

Two accounts really do collide here, and it is not exotic:
`project_id = sha256(root_path)`, so two CI boxes at `/home/ci/work` compute
the same id. With a single-column key that would be one row, whoever pushed
last would overwrite the other, and the unique index on `root_path` would turn
a coincidence into an error that told account A something true about account
B's disk.

### `GET /v1/personal/pull`

```
GET /v1/personal/pull?table=event&limit=1000&cursor=…
```

`200`:

```json
{
  "table": "event",
  "columns": ["id", "session_id", "…"],
  "rows": [["ev_…", "se_…", "…"]],
  "limit": 1000,
  "nextCursor": "eyJrIjpbImV2XzEyMyJdfQ"
}
```

Rows are ordered by the table's key columns, which is what makes the cursor a
keyset. **Not filtered by host**: the point of a pull is to see the other
machines, exactly as `sync.pull` does. It is filtered by account, on every
table, with no exception and no parameter that can turn it off.

### `GET /v1/personal/status`

```json
{
  "hosts": [ { "hostId": "h_…", "hostname": "studio", "os": "darwin",
               "firstSeen": …, "lastSeen": … } ],
  "tables": [ { "table": "event", "rows": 187431 } ],
  "totalRows": 191475
}
```

The counterpart of `cci sync status`. Counts are this account's rows only, so
they are comparable with the local database's counts and a mismatch means work
to do.

## 4. The team store

The redacted projection and nothing else. Its shape is `redact.Repo`,
`redact.Session` and `redact.Span` — keyed on `repo_id`, carrying an `actor`,
**with no path column in the schema at all.** Not nullable and unused: absent.
`docs/REDACTION.md` §2 is the reason — the shared database cannot leak what it
never received, and a column that exists is a column something can be written
into.

### The boundary

> A row may be published only if it belongs to a repo, and only to people who
> can already see that repo.

Concretely, an account's **scope** has two halves, because "which repos can I
see" and "whose rows can I see in them" are not the same question.

**Repos where every row is readable.** Access to the repository, in the sense
the rule above uses. Two sources, and both were *checked* somewhere:

1. Repos the account's GitHub identity was verified to reach at its last
   sign-in (§4.6).
2. Repos on the roster of a team the account belongs to — **to the extent the
   admin who added the repo had access to it.** A roster hands a team what
   that admin had: everything, if they were forge-verified for the repo;
   otherwise their own rows and nobody else's. See §4.5.

**Repos where only some rows are readable.** Rows the account published
itself. You can always read back what you sent, and a first publisher must be
able to check their own data before anybody else looks at any of it — but that
is a grant on the *rows*, not on the repository.

> **Publishing is not a grant.** A `repo_id` is
> `sha256("repo" + normalized_remote)`, and `docs/REDACTION.md` §0 establishes
> that publishing it is safe *because* the remote is already public on the far
> side. The flip side is that anyone who can guess the remote can compute the
> id. So a self-asserted `repo_id` is evidence of nothing, and registering one
> puts nobody in scope for anybody else's rows. This was wrong in the first
> implementation and it was a critical disclosure: two HTTP calls returned a
> stranger's sessions, actors, branch names and totals.

The consequence worth stating plainly: on an instance with **no GitHub app
configured**, no account ever gains full sight of a repo it did not publish
to. Rosters still work and still share — each admin shares their own rows —
but the system fails closed rather than trusting what a client claimed.

Every team endpoint computes that set first and intersects everything it
touches with it. There is exactly one function in the server that produces it,
and every query goes through it, because two implementations of this would
eventually disagree about the one thing that must not be wrong.

### 4.1 `POST /v1/team/publish`

Accepts one part of a `redact.Publication` per request, mirroring the personal
push so a client has one batching loop rather than two.

```json
{ "kind": "sessions", "actor": "lunowe", "rows": [ … ] }
```

`kind` is `repos` | `sessions` | `spans` | `withheld`. Row shapes:

```ts
// kind: "repos" — redact.Repo
{ repoId, remoteUrl, forge, owner, repo, webUrl, name }

// kind: "sessions" — redact.Session
{ sessionId, repoId, actor, source, gitBranch, startedAt, endedAt,
  activeMs, eventCount }

// kind: "spans" — redact.Span
{ spanId, sessionId, threadRole, startedAt, endedAt, eventCount }

// kind: "withheld" — the three counters on redact.Publication
{ hostId, withheldMs, withheldProjects, publishedMs }
```

`200 { "kind": "sessions", "received": 500, "applied": 500, "rejected": 0 }`.

- Max 5000 rows; `413 batch_too_large` above it.
- Idempotent, for the same reason the personal push is.
- Order: `repos` before `sessions` before `spans`. Out of order is
  `409 foreign_key_violation`. **The check is per-caller**: a `sessions` batch
  needs a `repos` batch *from this account*, not merely a repo somebody else
  registered. Asking globally made this endpoint an existence oracle — a 200
  rather than a 409 for a guessed `repo_id` answered "does anybody here work
  on `acme/skunkworks`", with the missing ids echoed back in the message.
  §0's "the remote is public on the far side" holds for a public repo and is
  untrue for a private one.
- `threadRole` must be one of `human` | `autonomous` | `unattended_root`.
  Anything else is `400 invalid_thread_role`. The three-bucket partition is the
  point of the field, and a fourth value silently entering the store would make
  every breakdown stop adding up.
- **`actor` is checked, not trusted.** It must equal the authenticated
  account's actor; otherwise `403 actor_mismatch`. The server could simply
  overwrite it, and that would be worse: a client whose projection says one
  thing while the store says another is a client whose `cci privacy` output no
  longer describes what it sent.
- A row naming a `sessionId` published by a different account is rejected and
  counted in `rejected`. Session ids hash a log UUID and are unguessable, so
  this should never fire; it is here because "should never" is how the
  confirmation attack in `docs/REDACTION.md` §0 survived into a design.
- `gitBranch` may be `null`, and a client that has switched branch names off
  locally simply sends `null`. See §4.5 for the server-side switch, which is a
  different control for a different person.

### 4.2 `GET /v1/team/repos`

Every repo in the caller's scope, with why it is there.

```json
{
  "repos": [
    { "repoId": "repo_…", "remoteUrl": "https://github.com/lunowe/harbor-cli",
      "forge": "github", "owner": "lunowe", "repo": "harbor-cli",
      "webUrl": "https://github.com/lunowe/harbor-cli", "name": "harbor-cli",
      "via": ["team:tm_…", "published", "github"],
      "branchNamesPublished": true }
  ]
}
```

`branchNamesPublished` is the effective value for *this caller* — false if any
team through which they can see the repo has it switched off. Off wins, because
the switch exists to stop a branch name being shown and a second team having it
on would defeat that.

A repo outside the caller's scope is not in this list and is not addressable
anywhere else in the API. There is no endpoint that takes a `repoId` and tells
you it exists. **A repo the caller merely registered is not in scope** — only
one they have verified access to, or have actually published rows into.

For a repo the caller reaches only as its publisher, the metadata is *what
that caller sent*, not the shared registry row. Otherwise the response would
differ depending on whether somebody else's row was already there, and that
difference is an answer to "does anybody here work on this repo". The shared
row is refreshed only by a caller with verified access to the repo; anyone may
create it, because creating a row that did not exist overwrites nothing.

### 4.3 `GET /v1/team/summary`

The scope-aware aggregate. **Computed at request time, inside the caller's
scope.** `docs/ACCOUNTS.md` §5 forbids the obvious optimisation: a nightly
rollup of "Alice: 40 h this week" that spans a repo Bob cannot see leaks that
repo's existence the moment Bob reads the total. There is no rollup table in
this schema, and adding one keyed by anything other than (viewer scope, period)
is a privacy defect, not a performance improvement.

Filters, all optional, all intersecting:

| param | type | meaning |
| --- | --- | --- |
| `repo` | repeated | `repoId`. Omitted = the whole scope. **An id outside the scope is ignored, not an error** — erroring would answer "does this repo exist". |
| `actor` | repeated | actor name |
| `source` | repeated | `claude_code` \| `codex` \| `opencode` |
| `role` | repeated | `human` \| `autonomous` \| `unattended_root` |
| `from` | epoch ms | inclusive lower bound on span start |
| `to` | epoch ms | exclusive upper bound on span start |

```json
{
  "scope": { "repos": 29, "teams": 1 },
  "activeMs": 642960000,
  "sessions": 320, "spans": 1430,
  "byRole": { "human": …, "autonomous": …, "unattendedRoot": … },
  "bySource": [ { "source": "codex", "activeMs": … } ],
  "byRepo": [ { "repoId": "repo_…", "name": "harbor-cli", "activeMs": … } ],
  "withheld": { … see §4.4 … },
  "generatedAt": 1789913704558
}
```

`byRole` partitions `activeMs` exactly, which is the property `thread_role`
travels as a label to preserve.

### 4.4 Withheld time, and what is deliberately not reported

`docs/ACCOUNTS.md` §5 adds a third rule to the two from the roadmap:

> **Withheld time stays counted.** A dashboard that silently drops part of
> someone's week is not private, it is wrong, and the person reading it cannot
> tell the difference.

So every aggregate carries a `withheld` block:

```json
"withheld": {
  "byActor": [ { "actor": "lunowe", "withheldMs": 52920000,
                 "withheldProjects": 21, "asOf": 1789913704558 } ],
  "totalMs": 52920000,
  "scope": "corpus",
  "rangeFiltered": false
}
```

`byActor` holds people with at least one span in the caller's scope, plus the
caller themselves — and that membership is **not** narrowed by `from`/`to`.
Narrowing it would mean somebody whose visible week happened to be empty
silently loses their withheld figure, which is rule 3's exact failure arriving
through the filter instead of through the schema.

Three things about it are load-bearing and none of them are obvious.

**It is a corpus total, not a range total, and it says so.** `redact` reports
withheld time as one number over the whole local database — it has no time
dimension, because the work it describes has no repo to hang a query on. A
renderer must therefore label it as "all time" even when the rest of the page
says "this week". `rangeFiltered: false` is in the payload so the renderer can
do that without knowing the reason.

**`publishedMs` and `totalMs` from the projection are never served to anyone
but their own author.** The client sends them, the server stores them, and a
teammate does not get them. They are corpus-wide across every repo that account
published, including repos the caller cannot see, so serving them would leak
the *magnitude* of invisible work — a weaker version of exactly the leak rule 1
exists to prevent. `GET /v1/team/summary` returns them only in the `byActor`
entry whose actor is the caller's own.

**There is consequently no "out of scope" number, and there must not be.**
`activeMs` is what the caller can see; `withheldMs` is what nobody can see.
They do not add up to the actor's total, and the difference — work in repos the
caller lacks access to — is not reported, not as a figure and not as a boolean,
because a boolean saying "there is more" is still an answer to "does a repo I
cannot see exist". A renderer must label `activeMs` as *in repos you can see*
rather than as a total.

`withheldMs` is **summed** across an actor's machines and `withheldProjects`
is the **maximum**, because they are not the same kind of quantity. Time on
two laptops is two disjoint stretches of somebody's week and adds up. Projects
are not disjoint: `project_id = sha256(root_path)`, so the same checkout path
on two machines is one project counted twice. There is no exact answer
available and there must not be — de-duplicating would need the project ids,
and a `project_id` *is* a path, which is the one thing this store never
receives. The maximum is a figure no machine's own report contradicts.

That `withheldMs` itself is disclosed is a deliberate trade, sanctioned by rule
3: it names no repo and cannot, since the whole definition of withheld work is
that it belongs to no repo. What it discloses is that a person has some
unpublishable time, which is the thing the rule says a viewer must not be left
to guess about.

### 4.5 Teams, rosters and the branch-name switch

| endpoint | who | does |
| --- | --- | --- |
| `GET /v1/teams` | member | teams the caller belongs to, with their role |
| `POST /v1/teams` | any account | create a team; the creator becomes `admin` |
| `GET /v1/teams/{teamId}/members` | member | `[{accountId, actor, role, joinedAt, invitedByActor}]` |
| `DELETE /v1/teams/{teamId}/members/{accountId}` | admin, **or yourself** | `204` |
| `POST /v1/teams/{teamId}/invites` | admin | mint a join code → `201` |
| `GET /v1/teams/{teamId}/invites` | admin | the codes, **without the codes** |
| `DELETE /v1/teams/{teamId}/invites/{inviteId}` | admin | `204`, idempotent |
| `POST /v1/teams/join` | any account | `{code}` → `200`, joins the code's team |
| `GET /v1/teams/{teamId}/repos` | member | the roster |
| `POST /v1/teams/{teamId}/repos` | admin | `{repoId, branchNamesPublished?}` → `201` |
| `PATCH /v1/teams/{teamId}/repos/{repoId}` | admin | `{branchNamesPublished}` → `200` |
| `DELETE /v1/teams/{teamId}/repos/{repoId}` | admin | `204` |

Any of these against a team the caller is not in: `404 not_found`. A member
using an admin route: `403 not_an_admin` — here the caller already knows the
team exists, so there is nothing to leak by being specific.

An admin may not remove the last admin from a team: `409 last_admin`. A team
nobody can administer is a roster nobody can correct, and the repos on it stay
visible forever. That applies to **leaving** exactly as it does to being
removed, which is why leaving is the same route rather than its own: a rule
with two implementations is a rule with one of them out of date.

### 4.5.1 Join codes — the only way onto a roster

**`POST /v1/teams/{teamId}/members` is gone.** It took an `accountId` and
added that person, and it was wrong twice over: nobody consented, and the
admin had no way to learn the id anyway — `account_id` is an opaque handle,
there is no directory endpoint, and there must not be one, because a lookup
from a name to an account id is an enumeration oracle over everybody on the
instance.

What replaces it is a code an admin mints and the colleague redeems **with
their own bearer token**. The redemption is the consent; the account id never
has to be discovered; and there is a record at both ends.

```jsonc
// POST /v1/teams/{teamId}/invites   (admin)
// body — every field optional
{ "role": "member", "expiresInMs": 259200000, "maxUses": 1, "note": "contractors" }
// 201 — THE ONLY RESPONSE THAT EVER CARRIES THE PLAINTEXT
{ "inviteId": "inv_…", "code": "ccij_…", "teamId": "tm_…", "role": "member",
  "createdAt": …, "expiresAt": …, "maxUses": 1, "note": "contractors" }

// POST /v1/teams/join            (any authenticated account)
{ "code": "ccij_…" }
// 200
{ "teamId": "tm_…", "name": "Platform", "role": "member", "alreadyMember": false }
```

The properties, each of which has a test that fails without it
(`server/tests/test_join_codes.py`):

- **Hashed at rest**, sha256, exactly as `api_token` is and for the reasons in
  `tokens.py`. The plaintext is in the `201` above and nowhere else — not in a
  log, not in the database, and not recoverable. `GET …/invites` therefore
  cannot re-show a code, which is a property rather than a gap: an endpoint
  that could would make one compromised admin session a way to recover every
  live invite on the instance.
- **256 bits**, from `secrets.token_urlsafe(32)`, prefixed `ccij_`. Long and
  opaque rather than short and typeable, unlike the device flow's `43CA-9AAA`:
  that one is read off a screen and typed within minutes against a throttled
  upstream, whereas this is pasted into Slack, lives for days, and **there is
  no attempt throttle on this server**. With nothing to make guessing
  expensive, entropy is the only defence.
- **Expiring, revocable, use-limited.** Defaults are **single-use** and
  **72 hours**; `maxUses` caps at 50 and `expiresInMs` at 30 days. Both are
  clamped rather than rejected — the same treatment `limit` gets in §0 — and
  the response echoes what was actually minted. There is no way to mint a code
  with no deadline, because nobody revokes a code they have forgotten.
- **Authentication is required to redeem.** The code says *which team*; the
  bearer token says *who is joining*. A code alone must never mint an
  identity, or whoever finds it in a Slack export is a member rather than a
  stranger holding a string.
- **Every failure is one failure.** Unknown, malformed, revoked, expired and
  exhausted are all `404 not_found` with one sentence, which names neither the
  code nor the team. A distinguishable "that code has expired" confirms the
  code was real, which confirms the team is real, to somebody who has just
  demonstrated they were not invited to it — §0's 403-versus-404 rule applied
  to a credential instead of a row.
- **Redeeming never changes an existing membership.** Already on the team is a
  `200` with `alreadyMember: true` that consumes no seat, so a retried request
  is free. It does **not** apply the code's role: if redeeming an `admin` code
  promoted an existing member, a leaked one would be a self-promotion route
  for everybody already inside. Promotion stays an admin action about a named
  person.
- **A seat is spent per join, not per person.** Somebody removed from the team
  who redeems the same code again spends another seat, so a spent single-use
  code is not a standing back door that outlives their removal.
- **Audit at both ends.** `createdBy` and `createdAt` on the invite,
  `{accountId, actor, redeemedAt}` per redemption, and `invitedByActor` on the
  member row. The redemption log is append-only and survives the member
  leaving — somebody joining, reading a week of data and leaving is precisely
  the sequence an admin needs to reconstruct afterwards.

**Joining does not widen anything by itself,** and that is the property this
design is really about. A new member sees exactly what the team's rosters
already delegated — bounded, as ever, by `team_repo.added_by` — and the
existing members see nothing new of the joiner. `scope.resolve` reads
`team_member` only to find which rosters apply; co-membership is a join key
and never a grant. `server/tests/test_invite_disclosure.py` attacks both
directions, including the roster escalation with an invite bolted on as a
fifth step.

The one visible change joining can cause is to the branch-name switch: a
caller inside a team is governed by their own teams (§4.5), so joining a team
whose roster has branch names *on* can lift a suppression another team's admin
had imposed. That is confined to a repo the caller could already read every
row of, on a field publishable under the repo rule anyway — anyone with repo
access can run `git branch -r` — and it can never uncover a name in a repo
joining did not otherwise reach.

**An admin may only add a repo that is already in their own scope.** This rule
is not in `docs/ACCOUNTS.md` and it has to be, because without it the roster
is a hole straight through the boundary. `repo_id` is
`sha256("repo" + normalized_remote)`, and `docs/REDACTION.md` §0 establishes
that publishing it is safe *because* the remote is already public on the far
side — which is exactly what makes the id computable by anyone who can guess
the remote. If `POST /v1/teams/{teamId}/repos` accepted an arbitrary id, then
knowing that somebody works on `github.com/acme/secret` would be enough: hash
it, add it to a team of one, and read their sessions. A repo the caller cannot
see is `404 not_found`, identical to one that does not exist.

A roster may widen **who** sees a repo. It may never widen **which** repos the
person doing the widening can see.

**And a roster grants only what the admin who added the repo had.** The scope
check above cannot carry this on its own, because a legitimate first publisher
rostering their own repo and an attacker rostering a guessed id are
indistinguishable at the moment they do it — both can "see" the repo, in the
row-level sense of §4. So the grant is delegated rather than absolute: if the
adder was forge-verified for the repo, the team sees every row in it; if not,
the team sees the adder's own rows. Delegation does not chain — a roster
passes on the adder's *verified* access only, because following a graph of
rosters to decide a disclosure is a traversal nobody can check by reading it.
The under-sharing that results is fixed by one admin with repo access adding
the repo again.

`DELETE` on a roster entry is idempotent and never 404s. Removing access is
not made harder than granting it.

**The branch-name switch** is `docs/ACCOUNTS.md` §5 rule 2 and
`docs/REDACTION.md` §5's last open item. Branch names are publishable under the
repo rule — anyone with repo access can run `git branch -r` — but they are free
text, and `feat/restricted-org-dbs` can say more than its author meant.

- Default is **on**, per the rule.
- It is a switch per (team, repo), not per repo globally, so an admin of one
  team cannot change what another team sees.
- It takes effect **at read time**. That is a deliberate exception to this
  project's "enforce at write" discipline, and it is the only one: the switch's
  entire purpose is that somebody can flip it *after* the rows were published,
  and a write-time-only control would do nothing for the branch names already
  in the store.
- The enforcement is still structural rather than a mask a query can forget.
  Branch names are stored in their own table, so a query that does not join it
  cannot return one. There is no `git_branch` column on the session row to
  accidentally select.
- When it is off, `gitBranch` is `null` in every response — the same value a
  session that never had a branch carries, so a renderer needs no new case and
  no observer can tell "suppressed" from "absent".

### 4.6 GitHub-derived repo access

`docs/ACCOUNTS.md` says team scope is *derived* from repo access rather than
hand-maintained. That is done at sign-in and only then: when the device flow
completes, the server has the person's GitHub token for the length of one
request, lists the repositories that token can reach, stores the resulting
`repo_id`s — `redact.repo_id(normalized_remote)`, the same hash the client
computes — and **discards the token**. Nothing about the person's GitHub
account is kept except the numeric subject id and the login.

The consequences are worth stating because they are visible to a user:

- Access is as fresh as the last sign-in. Losing access to a repo does not
  revoke the view of it until the next `cci login`. `docs/REDACTION.md` §5
  already says revocation is not retroactive; this makes the lag explicit
  rather than instantaneous-in-theory.
- **A sign-in that enumerates zero repos revokes.** GitHub answering "none" is
  an answer, and the offboarded contractor running `cci login` is precisely
  the moment it should land — tokens here do not expire, so a sign-in that
  does not revoke means nothing ever will.
- **A sign-in that could not enumerate changes nothing.** An outage, a rate
  limit, a token whose scopes do not cover repositories, or a failure part-way
  through pagination: the stored list is kept, `verifiedAt` is not restamped,
  and whatever *was* successfully listed is added. An empty list and a failed
  enumeration are the same value and opposite facts, and treating them alike
  meant either wiping a scope for the length of an incident or — the direction
  it actually failed in — never revoking at all.
- If the token's scopes cover only public repositories, only those are
  verified. Everything else still works through team rosters, which is why the
  roster is not optional.
- This layer is additive. An instance whose GitHub app is not configured for it
  runs on rosters alone and behaves identically in every other respect.

### 4.7 `GET /v1/team/sessions`

Paginated session rows inside the caller's scope. Same filters as
`/v1/team/summary`, same pagination as §0.

```json
{
  "sessions": [
    { "sessionId": "se_…", "repoId": "repo_…", "actor": "lunowe",
      "source": "codex", "gitBranch": "main",
      "startedAt": …, "endedAt": …, "activeMs": …, "eventCount": … }
  ],
  "limit": 1000,
  "nextCursor": null
}
```

`gitBranch` is `null` when the switch in §4.5 is off for the caller.

There is no `/v1/team/events`. Events are not published at all — 187 k rows
whose analytical value is already carried by spans, and `tool_name`/`model` is
a finer-grained behavioural picture of a person than a team view has any
business holding. `redact.FIELDS` classifies every one of those columns
`PRIVATE`; this is the same decision, at the other end of the pipe.

### 4.8 `GET /v1/team/actors` and `GET /v1/team/daily`

```json
// GET /v1/team/actors — one row per person with visible time
{ "actors": [ { "actor": "lunowe", "accountId": "acc_…",
                "activeMs": …, "sessions": …, "repos": … } ],
  "withheld": { … §4.4 … } }

// GET /v1/team/daily — UTC calendar days, no gap filling
{ "days": [ { "date": "2026-09-20", "activeMs": … } ],
  "withheld": { … §4.4 … } }
```

Both take the §4.3 filters and both are computed inside the caller's scope at
request time, with no precomputation anywhere. **Both carry the `withheld`
block**, as §4.4 says every aggregate does. `/daily` shipped without one, and
it is the endpoint that most needs it: a "hours this week" chart is built by
summing `days`, and without the block it shows a person's week with the
unpublishable part silently missing — `docs/ACCOUNTS.md` §5 rule 3's exact
failure, where the reader cannot tell a quiet week from a week spent in a repo
with no remote. The block is the same corpus figure with the same
`rangeFiltered: false` beside it, so a renderer drawing a daily axis can label
it "all time" without needing to know why there is no day to hang it on.

`/v1/team/daily` buckets by **UTC**, unlike `docs/API.md`'s `/api/daily`, which
buckets by the local calendar day. A team spans timezones and there is no local
day to agree on; the local dashboard has exactly one reader and there is. The
divergence is deliberate and a renderer must label the axis.

`docs/ACCOUNTS.md` §7 rules out anything derived about individuals beyond this:
no ranking, no "time saved", no per-person cost. This data can answer "who
worked the most hours", the answer is wrong — it measures agent time, not work
— and it will be quoted anyway.

## 5. Status codes

| code | when |
| --- | --- |
| `200` | fine |
| `201` | something was created and the body describes it |
| `204` | fine, nothing to say |
| `400` | malformed body, unknown column, bad filter, and every device-flow error |
| `401` | no token, or a token that is unknown, expired or revoked |
| `403` | authenticated, permitted to know the thing exists, not permitted to do it |
| `404` | absent — **or present and outside your scope**, indistinguishably |
| `409` | a real conflict: FK order, last admin, an id owned by someone else |
| `413` | batch above the row cap |
| `429` | rate limited. `Retry-After` in seconds. |
| `500` | a bug. Never carries detail; the detail is in the server log. |
| `502` | GitHub could not be reached |
| `503` | the database could not be reached |

## 6. Not in this version

Stated so the absences read as decisions rather than omissions.

- **Public signup, billing, plan limits.** One private instance was the
  decision in `docs/ACCOUNTS.md` §1.
- **Pointing the dashboard at the server.** The read path stays SQLite;
  `db.to_dialect` is deliberately not a general query translator.
- **Publishing cost.** Excluded from sync by design and classified closed for
  publication. A per-repo cost aggregate is a reasonable thing to want and
  there is no field for it; it needs its own decision, not a default.
- **Deleting published rows.** Revocation of access is a roster change. Erasing
  history is a different feature with different questions — whose copy, and
  what happens to an aggregate somebody already read — and inventing an answer
  here would foreclose them.
