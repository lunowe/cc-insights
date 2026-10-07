# CC-Insights

**C**oding-agent **Insights** — a local-first tracker and visualizer for how,
when, and how long coding agents run on this machine.

It reconstructs an accurate timeline of agent activity from the logs the agents
already write, so you can answer: when do I actually use agents and for how
long, how often do I run them in parallel, which projects consume the time, and
how much of it is me driving versus agents running on their own.

## Status — Stage 2 complete, v1 and v2 under way

The pipeline works end to end: three source adapters, incremental ingest, span
derivation, cost, a dashboard and a CLI. It runs on Windows, syncs between your
machines through PostgreSQL, and knows what it may and may not share with
anyone else. 953 tests.

## Install

One command, on a machine with nothing set up:

```bash
curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash
```

It finds a Python 3.11+ (and if there is not one, names the command that gets
you one on your platform), installs through `pipx` when you have it and a
managed venv under `~/.local/share/cc-insights` when you do not, links `cci`
into `~/.local/bin`, and finishes by running `cci install` and then
`cci doctor` — so the last thing on screen is either a green report or the
exact command that fixes what is wrong. It never prompts: piped from curl it
*cannot*, since stdin is the script itself. Re-running it upgrades in place.
Options go after `-s --`:

```bash
curl -fsSL .../scripts/install.sh | bash -s -- --watch         # live mode (macOS)
curl -fsSL .../scripts/install.sh | bash -s -- --join <link>   # then sign in and join a team
curl -fsSL .../scripts/install.sh | bash -s -- --server <url>  # then sign in to your own server
curl -fsSL .../scripts/install.sh | bash -s -- --uninstall
```

`--join` and `--server` end in a browser sign-in that waits for your
approval; see "Sharing with other people" for where a join link comes from.

If you would rather not pipe a script you have not read, this is the same
thing with the Python-finding left to you:

```bash
pipx install cc-insights
cci install
```

### With your coding agent

Paste [`docs/agent-setup-prompt.md`](docs/agent-setup-prompt.md) into Claude
Code, Codex or any other agent with a shell. It installs, checks
`cci doctor`, explains the first numbers, and then asks you about sync and
teams. Two things it always leaves to you: approving the `cci login` code in
your browser, and saying yes before `cci publish` sends anything.

The same guidance is also available as a skill, so the agent can help again
later. In Claude Code:

```
/plugin marketplace add lunowe/cc-insights
/plugin install cc-insights@cc-insights
```

For other agents, copy [`skills/cc-insights`](skills/cc-insights) into the
agent's skills directory.

### From a checkout

Contributors, and anyone installing before the first release is on PyPI. The
frontend build comes **first** — the wheel force-includes `frontend/dist`, so
without it even `pip install -e .` fails with `Forced include not found`:

```bash
pnpm --dir frontend install && pnpm --dir frontend build
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/cci install
```

`scripts/install.sh` run from inside a checkout installs *that checkout*
rather than PyPI, and builds the frontend for you if it is missing.

`cci install` does the whole first run — config, database, a first ingest, and
a background job — because every one of those used to be a separate thing to
remember and forgetting the last one is unrecoverable: **agent log directories
are pruned on a rolling basis, so a gap in capture is a permanent gap in
history.** That is the point of the project, not a footnote.

It installs a launchd job on macOS, a Task Scheduler job on Windows, and one
line in your crontab on Linux. It changes only that line, which ends in
`com.cc-insights`, and leaves the rest of your crontab as it was. With no
scheduler at all, for example in a container without cron, it prints the
line for you to schedule yourself. Either way the job is pinned to the config
directory you installed from, so `CC_INSIGHTS_HOME=/data/cci cci install`
captures into `/data/cci` and not into a second database under `~/.config`
that nothing reads.

To follow the logs live instead of every 15 minutes, `cci install --watch` —
one or the other, never both, since two writers on one SQLite database is the
one way to make this contend with itself. The installer enforces that rather
than documenting it. Watch mode is macOS-only: Task Scheduler and cron start
processes but do not keep them up, so on Windows and Linux `--watch` is refused with
a pointer to the 15-minute job rather than registering a watcher that would
stay dead after its first exit. `cci install --uninstall` removes either, and
keeps your data.

To check it is still working, at any point:

```bash
cci doctor
```

It reports the job, how long since the last ingest, the schema, and what to
run for anything that is not right — and exits non-zero on a real failure, so
it works from a script too. It measures freshness from when the tool last
*looked*, not from the newest event: no events for three days is a quiet week,
no ingest for three days is a broken install, and only the second is urgent.

Config and database live in `~/.config/cc-insights`, or
`%APPDATA%\cc-insights` on Windows. `CC_INSIGHTS_HOME` overrides both.

## Looking at it

```bash
cci stats     # the summary, in the terminal
cci serve     # the dashboard
```

`cci serve` opens the dashboard: filter by project, source, thread role and
date range, with the filter state in the URL so a view is shareable. The page
reads the live database through the API it is served from; if it is opened
from somewhere with no API behind it, it falls back to the bundled sample and
says so on the badge rather than passing the sample off as your data.

To watch it happen instead, `cci watch` follows the logs and keeps the database
current as the agents write it, ~0.15 s per cycle because ingest resumes at
each file's byte offset and only the sessions that moved are re-derived:

```bash
cci watch                 # follow the logs, print a line per change
cci watch --serve         # ...and a dashboard that refreshes itself
cci install --watch       # ...or leave it running in the background
```

Install one background job or the other, not both: two writers on one database
is the one way to make this contend with itself. `cci install` unloads the
other before it loads either, and `--uninstall` removes both.

### Several machines

Each machine ingests its own logs locally, pushes the rows it owns, and pulls
the others' back down — local SQLite stays the source of truth and the
dashboard never changes. There are two ways to be the thing in the middle,
and both are supported.

**Sign in.** The short path, and the one the background job uses:

```bash
cci login --server https://your-instance   # device code; approve it in a browser
cci sync push                              # send this machine's rows
cci sync pull                              # bring the other machines' rows down
cci sync status                            # who has pushed what
```

`cci login` claims this host for your account and stores the token in
`credentials.toml`, mode 0600 — never in `config.toml`, which is plain text
people copy around. After that the 15-minute job pushes on its own, and
`cci logout` clears and revokes the credential.

**Or run your own PostgreSQL.** Still a first-class mode, not dead code:

```bash
pip install -e '.[postgres]'
export CC_INSIGHTS_SYNC_URL=postgresql://user@host/cci   # or sync_url in config.toml
cci sync push --direct
```

A configured `sync_url` wins over being signed in, `--account` and `--direct`
force either, and every command prints which one it used.

Re-running any of it is safe: every id is a content hash, so a row that
crosses twice collapses instead of duplicating. A push that is interrupted
resumes rather than restarting, and a push with nothing new to say costs zero
requests. `ingest_file` is deliberately never sent — it is bookkeeping about
local paths with no analytical value.

### Sharing with other people

`sync` moves paths, because it is all your own disk. Sharing beyond one person
goes through a different pipe, and a different set of rules:

```bash
cci privacy        # what would and would not cross a team boundary. Sends nothing.
cci publish        # send the redacted projection. Asks first, and says what it withholds.
cci team           # the repos in scope, and the scope-aware summary
```

Teams themselves are `cci team <verb>` — `list`, `new`, `members`, `repos`,
`share`, `unshare`, `branches on|off`, `sessions`, `actors`, `daily`. Getting
a colleague in takes two commands and two people:

```bash
cci team invite            # you, an admin: mints a join link, printed once
cci team join <link>       # them: signs in to your server if needed, then redeems
```

The link is `https://<your-server>/join/<code>`, so it carries the one thing a
fresh machine is missing: which server to sign in to. There is deliberately no
default server — a public package that signed everybody in to one instance
would make that instance a hosted service. A colleague with nothing installed
runs the installer with the link instead, and it installs, signs them in and
joins in one go:

```bash
curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash -s -- --join <link>
```

Opened in a browser, the link shows those same two commands and nothing else:
the page never looks the code up, so it cannot confirm that a team exists. For
your own second machine, `--server <url>` in place of `--join` installs and
signs in.

The redemption *is* the consent — there is no way to add somebody by id, and
no way to look an account id up. The code is a bearer secret: 256 bits, stored
hashed, single-use and good for three days unless you say otherwise, and shown
exactly once because it genuinely cannot be shown again. `cci team invites`
lists what is outstanding and who redeemed what; `cci team revoke <id>` kills
one that went astray.

Joining a team does not widen what anyone can see on its own. A new member
sees what the team's rosters already shared, and the existing members see
nothing new of them — `cci team share` is the only command that changes what
other people can read.

`docs/REDACTION.md` has the design. The short version: publishing `project_id`
publishes `root_path`, because the id *is* `sha256(root_path)` and a colleague
can hash a guess — 555 guesses recovered 20% of this corpus. So published rows
are re-keyed on the repo's remote, the boundary is repo access, and work with
no remote stays local, counted rather than silently dropped. The projection
runs on your laptop; the shared database never receives a path.

`cci publish` prints what it is withholding *before* it sends anything, and
asks for confirmation the first time on each machine — it is the one command
that puts your data somewhere other people read, so it should not be one you
can run by accident. It refuses outright while the projection fails its own
audit, or while any schema column is unclassified.

`docs/ACCOUNTS.md` is the design and `docs/SERVER_API.md` is the frozen wire
contract: one private instance, two stores rather than one filtered on read,
GitHub device-code sign-in, and a `cci login` that makes a second machine a
sign-in instead of a database URL.
## What it costs

```bash
.venv/bin/cci price sync   # load rates for the models you actually ran
.venv/bin/cci cost         # the breakdown
```

**$5,658 at published API rates** on this corpus, and the shape of it is the
finding: **cache reads are half** of the total, cache writes 28% (three-fifths
of that at the one-hour rate), output 19%, fresh input 3%. The cheapest
component per token is most of the bill.

Each API response is counted once. Claude Code writes one log line per
content block — thinking, text, each tool call — and repeats the response's
whole `usage` on every one of them, and Codex repeats usage events too. An
earlier version summed what it read and reported **$11,630** for the same
corpus, about twice the real figure. `scripts/parity_ccusage.py` compares the
per-model token totals with [ccusage](https://github.com/ccusage/ccusage)'s;
on this machine Claude Code matches token for token, and Codex differs only by
the compaction requests the published ccusage release does not count yet.

That figure is a *list-price equivalent*, not a bill — a subscription charges a
flat monthly fee no matter how many tokens run through it. Rates come from a
committed snapshot of [LiteLLM's price list](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json),
the same catalog [ccusage](https://github.com/ccusage/ccusage) uses, with
[models.dev](https://models.dev) filling in a model LiteLLM lacks. They are
keyed by model **and date**, so a vendor's next price change does not rewrite
last month: LiteLLM publishes no dates, so `scripts/sync_prices.py` keeps the
old rate and dates the new one the day it first saw it, and prints every change
for a human to check. Anything it cannot price is reported rather than counted
as zero — free tiers such as opencode's `*-free` models stay unpriced rather
than borrowing the paid model's rate.

A model is matched by the name the log recorded: the exact key first (also
without an `anthropic/` or `openai/` prefix or a date suffix), then the longest
key the name contains — but never across a version number, so
`claude-opus-5-5` can only be priced as itself and never as `claude-opus-5`
($4/$20 against $5/$25 per MTok), and never across a size tier, so `o1-mini` is
not priced as `o1`. A name priced through a near relative is listed beside every
total.

Cache writes are priced at two rates, because they have two lifetimes: 1.25x
base input for a five-minute write and 2x for a one-hour one. 43% of this
corpus's cache writes take the expensive one — Claude Code gives the main conversation an
hour on a subscription within plan usage, and five minutes to subagents — and
the logs record which, so `cci cost` does not have to guess. A database built
before that column existed fills it with `cci backfill`, from the logs that
are still on disk.

Three layers, most specific first: the catalog, then
`src/cc_insights/price_overrides.json` — corrections checked against the
vendor's own pricing page and shipped with the code — then `cci price set`,
which a human owns and no sync touches. The middle layer exists because a
catalog's errors are not small and a fix kept in one laptop's database is
lost on the next machine: the previous catalog priced **Claude Fable 5.1 as
Fable 5**, whose cache reads cost four times as much ($1.00 against $0.25 per
MTok), which was **$1,151 — a fifth of the total** — and it carried a Sonnet 5
price rise that never happened. LiteLLM gets both right, so those corrections
are retired (the file says why) and the layer is empty until the next one is
needed; `cci price sync` says when upstream agrees with a correction.

## What it found on this machine

185 hours of active agent time across 223 days, from 306 sessions and 714
threads. The measurements are real; the project names throughout this repo's
docs are invented, and the dashboard's sample data is synthetic:

Projects are the logical unit: a repo's worktrees and subdirectories fold into
one. `atlas-chat` reads **111.6 h across 13 paths**, seven of which no longer
exist on disk — flat, it looked like 54.5 h.

| | Claude Code | Codex |
| --- | --- | --- |
| Active time | 143.8 h | 41.5 h |
| Threads | 499 | 215 |
| Wall-clock with ≥1 active | 83.6 h | 36.1 h |
| **Parallelism multiplier** | **1.72x** | 1.15x |
| Peak concurrent threads | **7** | 4 |
| Time with ≥2 running | 34.1 h (41%) | 3.3 h (9%) |

Split by who started the work:

| | hours | |
| --- | --- | --- |
| human-initiated | 98.4 h | 53% — a person typed the turn that started it |
| autonomous | 81.4 h | 44% — a model spawned it (subagent threads) |
| unattended root | 5.5 h | 3% — agent ran on in the main thread |

## Three things that make the numbers trustworthy

**Wall-span is not usage.** Summed session spans come to 2,710 h against 143 h
of real activity — 2.4%. Sessions sit open for days. Active time is the sum of
*span* durations, where a span breaks on any gap over the idle threshold, and a
gap above the threshold contributes **zero** rather than a capped value.

**The idle threshold is not a tuning knob.** The measured gap distribution has a
wide flat valley between "agent is working" (p99 = 216 s) and "user left". Any
threshold between 120 s and 900 s gives materially the same totals.

**Every number is reproduced from one reference implementation.**
`docs/probes/canonical_metrics.py` is the spec; the derivation engine is tested
for exact agreement with it, compared in a single process rather than against a
frozen constant — the corpus grows while you measure it. That test exists
because three published "measured" numbers turned out to be bugs, each caught by
an implementer who refused an acceptance number they could not reproduce. See
`docs/FINDINGS.md` §5.

## Design stance

- **Metadata only.** Prompt and response text is *never stored* — only
  timestamps, ids, cwd, branch, model, token counts, tool names. `RawEvent` is a
  frozen slots dataclass so an adapter structurally cannot smuggle content
  through it. This is what makes the future multi-machine mode safe.
- **Portable by design.** SQLite now; `host_id` on every row and epoch-ms
  timestamps throughout, so Postgres is a backend swap, not a migration.
- **Ingest before dashboards.** Logs are a rolling window. Capturing history is
  urgent; visualizing it is not.

## Layout

```
src/cc_insights/
  sources/      adapters: claude_code.py, codex.py, opencode.py (contract in base.py)
  ingest.py     adapters -> DB, incremental and idempotent
  derive.py     active spans, attendance, concurrency
  pricing.py    the dated rate table, from a committed price catalog
  cost.py       event -> money, and what it could not price
  watch.py      follow the logs; the tick behind a live dashboard
  metrics.py    filter-aware queries, one per API endpoint
  serve.py      the read-only localhost server
  stats.py      read-only summary queries
  cli.py        cci install | doctor | ingest | derive | cost | price |
                watch | serve | sync | privacy | ...
  scheduler.py  installing the background job, from the installed package
  doctor.py     is this installation actually capturing anything
  paths.py      path reasoning that takes the OS from the path, not the host
  sync.py       push/pull between local SQLite and a shared PostgreSQL
  redact.py     what may cross a team boundary, and in what shape
  assets.py     where the shipped dashboard, migrations and job templates are
  jobs/         launchd + Task Scheduler templates, package data so `cci`
                can install a job on a machine with no checkout
  model_prices.json   the price catalog snapshot (scripts/sync_prices.py)
frontend/       Vite + React dashboard, built into frontend/dist -- the wheel
                force-includes it as cc_insights/web
migrations/     numbered SQL, applied in order; also force-included
docs/           FINDINGS.md (ground truth), API.md (frozen contract),
                REDACTION.md (what may be shared), ACCOUNTS.md (teams,
                designed not built), ROADMAP.md, probes/,
                agent-setup-prompt.md (the copy-paste setup prompt)
skills/         cc-insights/ -- the agent skill; .claude-plugin/ publishes
                it as a Claude Code plugin
scripts/        install.sh -- the `curl | bash` bootstrap; install-launchd.sh
                -- the from-a-checkout wrapper; fixture and price sync
.github/        ci.yml (tests on push/PR) and release.yml (tag -> PyPI),
                both of which build the frontend before touching the wheel
```

## Releasing

Bump `version` in `pyproject.toml`, then push a matching tag:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

`.github/workflows/release.yml` does the rest, in this order and no other:

1. **Build the frontend.** `pyproject.toml` force-includes `frontend/dist`
   into `cc_insights/web`, and `frontend/dist` is gitignored because it is a
   build artifact — so on a fresh checkout it does not exist and hatchling
   refuses to build at all. Before that guard existed the failure was worse
   than a broken build: the wheel shipped *without* the dashboard, `cci serve`
   told people who had no repo to run `npm run build`, and nothing said the
   release was incomplete. Build the frontend, **then** the wheel.
2. **Run the full suite.** `tests/test_packaging.py` builds a wheel and looks
   inside it — dashboard, every migration, the job templates, the entry
   point. That is the gate; asserting on `pyproject.toml` would only confirm
   the rule is written down, not that hatchling honoured it.
3. **Build the wheel and sdist**, then re-check the artifact that is about to
   be uploaded. `test_packaging.py` *skips* its dashboard assertions when
   `frontend/dist` is absent, which is right on a laptop and would be a hole
   here, so the release job repeats the check in a form that cannot skip.
   The sdist carries `frontend/dist` too, for the same reason step 1 exists:
   a wheel built *from the sdist* force-includes it, so an sdist without it
   is not a dashboard-less install, it is `pip install --no-binary
   cc-insights` failing with `Forced include not found` — along with every
   distro that builds from source. `test_packaging.py` builds an sdist and
   then a wheel out of it, which is what pip does when no wheel matches.
4. **Publish via trusted publishing.** PyPI mints a short-lived token from the
   workflow's OIDC identity, so there is no API token in repository secrets.
   The publisher is configured on PyPI against this repository, the workflow
   filename `release.yml` and the `pypi` environment; all three are matched,
   so renaming the file breaks the upload.

The tag is checked against `project.version` before anything is built: PyPI
takes the version from the metadata and ignores the tag, so a mismatch
publishes a number the release notes disagree with.

`.github/workflows/ci.yml` runs the same tests on every push and pull request
— Linux on 3.11/3.12/3.13 plus one macOS leg, because the launchd half of
`scheduler.py` skips everywhere else.

## Roadmap

`docs/ROADMAP.md`. v1 has landed the opencode adapter, real cost tracking and
watch mode; session annotation (tagging a session client/ticket/billable after
the fact) is what remains. v2 is done bar teams: Windows, a per-machine probe
cache, PostgreSQL sync, and the redaction layer that had to be designed before
any data left a laptop. Teams need auth and scoping, not privacy plumbing.
Then outcome correlation. "Time saved" is explicitly deferred, and the roadmap
explains why.
