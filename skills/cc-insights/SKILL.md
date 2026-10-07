---
name: cc-insights
description: Install, set up and operate CC-Insights (`cci`), the local-first tracker for Claude Code, Codex and opencode usage. Use when the user wants to install cci or check it is capturing, read their agent time, cost or dashboard, sign in and sync several machines, publish to a team, create or join a team, or fix what `cci doctor` reports.
---

# CC-Insights

`cci` rebuilds a timeline of coding-agent activity from the logs Claude Code,
Codex and opencode already write: active time, parallelism, projects, who
drove (person or agent), and list-price cost. It stores metadata only, never
prompt or response text, in a local SQLite database.

**Capture is the urgent part.** Agents prune their log directories on a
rolling basis, so a gap in capture is a permanent gap in history. Setup is
done when a background job runs and `cci doctor` passes. Everything else can
wait.

## Ground rules

- **`cci doctor` decides.** Every line that is not `ok` names the command
  that fixes it. Run doctor after every change. A step is done when doctor
  ends with `everything is working.`, or shows only WARNs the user has
  accepted.
- **`--help` has the flags.** `cci <cmd> --help` is authoritative for every
  subcommand, including nested ones like `cci team invite --help`. This skill
  covers what `--help` doesn't say.
- **The config directory is irreplaceable.** That is `~/.config/cc-insights`,
  or `%APPDATA%\cc-insights` on Windows, or `$CC_INSIGHTS_HOME`. Its
  database holds the only copy of history the agents have already pruned.
  `host_id` in `config.toml` is part of every session id, so keep it exactly
  as written. `credentials.toml` holds a bearer token, so treat it like a
  password.
- **One writer per database.** Each machine runs exactly one of these: the
  15-minute job, the live job (`cci install --watch`), or a foreground
  `cci watch`. `cci install` swaps the two job types, so a re-run without
  `--watch` turns live mode back into the 15-minute job. With a job
  installed, view the data with `cci serve`, which is read-only.
- **The user consents; you prepare.** Two actions put data where other people
  can read it: publishing (`cci publish`) and handing out a join link. For
  both, show the user what will happen and act only on their explicit yes.
  Commands that need `sudo` belong to the user: give them the exact command
  and wait. A browser sign-in (`cci login`, `cci team join <link>`, the
  installer's `--join`/`--server`) blocks until the user approves a code:
  run it in the background and relay the code (references/sync.md, step 2).

## First setup

1. **Look for an existing install.** Run `command -v cci && cci --version &&
   cci doctor`. If doctor passes, go to step 4.

2. **Install.** First check whether the package is on PyPI:
   `curl -fsS -o /dev/null https://pypi.org/pypi/cc-insights/json`.
   - **macOS or Linux, on PyPI:**
     `curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash`
   - **macOS or Linux, not on PyPI yet (404):** clone the repo and run the
     installer from inside the clone, which installs that checkout:
     `git clone https://github.com/lunowe/cc-insights ~/cc-insights && bash ~/cc-insights/scripts/install.sh`.
     The first run builds the dashboard and needs `pnpm` or `npm`.
   - **Windows (PowerShell):** `pipx install cc-insights` (or, from a clone,
     build `frontend/` first and then run `pipx install <clone path>`). Then
     run `pipx ensurepath`, open a new shell, and run `cci install`. This
     registers a Task Scheduler job.

   The installer finds Python 3.11+, installs through pipx or a managed venv,
   links `cci` into `~/.local/bin`, then runs `cci install` (config,
   database, first ingest, background job) and `cci doctor`. It never
   prompts. Options go after `-s --` (or straight after `install.sh` in a
   clone):
   - `--join <link>`: the user has a join link from a team admin
     (`https://<server>/join/<code>`). The installer also signs in to that
     server and joins the team. The link is a password: pass it through,
     never echo it.
   - `--server <url>`: the user has their own account server. The installer
     also signs in to it.
   - `--watch`: follow the logs live instead of every 15 minutes (macOS only).

   With `--join` or `--server`, the installer ends in a browser sign-in that
   waits for the user, so run it in the background and relay the code as
   references/sync.md step 2 describes. If the sign-in times out, the install
   itself is still complete; re-run just `cci team join <link>` or
   `cci login --server <url>`.

   If the installer stops at `no Python 3.11+ found` or `no ensurepip`, it
   prints a fix that usually needs `sudo`. Hand that command to the user. On
   older LTS releases (Ubuntu 22.04, Debian 11), the system `python3` is too
   old, so suggest `python3.12` from the deadsnakes PPA, `pyenv` or `uv
   python install 3.12`. Check `python3.12 --version` before you re-run the
   installer.

   Done when its last line is `cc-insights is installed and capturing.`

3. **Fix PATH and check.** When the installer warns that its bin directory
   is not on PATH, it prints the exact line and the rc file. Append the line
   to that file and tell the user which file you edited. In the current
   shell, keep calling `cci` by its absolute path.

   The background job is a launchd job on macOS, a Task Scheduler task on
   Windows, and one crontab line ending in `com.cc-insights` on Linux.
   `cci install` writes that line itself and leaves the rest of the crontab
   alone. If the installer reports `no scheduler found`, the machine has no
   cron at all, which is common in containers. Have the user install cron
   (for example `sudo apt install cron`), then re-run `cci install`. If
   installing cron isn't possible, schedule the printed line with whatever
   runs jobs there.

   Check it in a fresh interactive shell, which reads the rc file you just
   edited: `$SHELL -ilc 'command -v cci && cci doctor'`. Done when that
   passes.

4. **First look.** Run `cci stats` and summarize the result for the user.
   Explain the numbers before they draw conclusions from them:
   - **Active time** is the sum of spans that end at any idle gap longer than
     `idle_threshold_s` (300 s by default). Hours a session sits open don't
     count, so active time is far lower than session wall-clock.
   - **Cost** (`cci cost`, after `cci price sync`) is a list-price
     equivalent at published API rates (LiteLLM's catalog, the one ccusage
     uses), dated per model. It is not a bill: a subscription charges a flat
     fee. Each API response is counted once, and per-model token totals
     match ccusage's. Unpriced models are reported, not counted as zero; free
     tiers such as opencode's `*-free` models are unpriced on purpose, and
     `cci price set <model> --input 0 --output 0` records them as free if the
     user wants. `database is locked` from `cci cost` means the background
     job is writing: retry after a few seconds.
   - If one repo shows up as many projects (worktrees, subdirectories), run
     `cci group auto --dry-run`, show the plan, then `cci group auto`.

   Dashboard: `cci serve` runs until stopped, so start it in the background.
   It opens http://localhost:8787 (`--port` changes the port, `--no-open`
   skips the browser).

5. **Offer the next steps** that fit the user: more machines, a team, or
   neither. Stopping at local-only is a complete setup. If the user has a
   join link and didn't install with `--join`, `cci team join <link>` does
   the sign-in and the join now (references/teams.md).

## Where to go next

- **Several machines:** signing in, `cci login`, `cci sync`, or your own
  PostgreSQL. See [references/sync.md](references/sync.md).
- **Teams:** `cci publish`, creating or joining a team, join links, sharing
  a repo, branch names. See [references/teams.md](references/teams.md). A
  join link signs in by itself; everything else there needs sign-in from
  `sync.md` first.
- **Something wrong:** a doctor FAIL, missing data, upgrading, uninstalling,
  or a custom data location. See
  [references/troubleshooting.md](references/troubleshooting.md).
