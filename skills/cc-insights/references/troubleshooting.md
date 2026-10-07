# Troubleshooting

Start with `cci doctor`. Every line that is not `ok` names its fix. A FAIL
means history is being lost or nothing works. A WARN means something is
behind or degraded. A non-zero exit code means a real FAIL. The cases
below are the ones where the named fix is not the whole story.

## Doctor lines

- **`background job` FAIL:** run `cci install` again, or
  `cci install --watch` if the user had live mode. It unloads any old job
  before loading the new one, so it is safe to re-run.
- **`background job` FAIL, `cannot read your crontab`:** `crontab -l` itself
  fails, and `cci install` refuses to write a table it could not read. Run
  `crontab -l`, fix what it reports (permissions, a stopped cron service),
  then run `cci install`.
- **`background job` FAIL, `no cron daemon is running`:** the line is in the
  crontab, but nothing will run it. This happens on WSL without systemd, and
  in containers. Hand the user `sudo systemctl enable --now cron` (`crond`
  on Fedora or Alpine).
- **`background job` WARN, `no scheduler here`:** there is no cron, launchd
  or Task Scheduler, so doctor can't see a job the user scheduled another
  way. Install cron and re-run `cci install` (SKILL.md, step 3).
- **`last ingest` stale (WARN after 1 h, FAIL after 24 h), but the job is
  `ok`:** `last ingest` records when a log file last *changed*, not when the
  job last ran. A day with no agent use therefore reads as stale on a
  healthy install. Before concluding anything is broken, ask whether the
  user ran an agent since then, and read `logs/ingest.err` in the config
  directory (`logs/watch.err` for the live job). Nothing in that file, plus
  no agent use, means the install is fine.
- **`agent logs: none found`:** this is the same diagnosis as `no events
  yet` below. Nothing is being captured.
- **`data: no events yet`:** run `cci config` to see the `source_globs` and
  compare them with where the user's agents actually write logs. Logs in
  non-default locations need their globs edited under `[source_globs]` in
  `config.toml`, followed by `cci ingest && cci derive`.
- **`dashboard` not built** (checkout installs only): run
  `pnpm --dir frontend install && pnpm --dir frontend build` in the checkout.
  The JSON API works without it.
- **`schema: pending migrations`:** this usually follows an upgrade. Run
  `cci init`.
- **`account` WARN, server unreachable:** capture continues locally. The
  push catches up once the server is back.
- **`account` FAIL, `rejected this credential`:** the token was revoked, or
  the server was reset and no longer knows the account. Run `cci logout`,
  then `cci login --server <url>` (the user approves in the browser). After a
  server reset the account and its teams are new: the admin recreates the
  team, re-shares repos, mints new join links, and each member publishes
  again. The next background run pushes this machine's history in full.
- **`sync`: a sync URL is set but the driver is missing:** install the
  postgres extra. See "Direct PostgreSQL" in [sync.md](sync.md).

## Install and PATH

- **`bad interpreter` from `cci`:** the Python the venv was built with was
  upgraded out from under it, which Homebrew does on minor-version bumps.
  Re-running the installer rebuilds the venv.
- **`your shell finds a different cci first`:** there are two installs.
  Remove the one the user doesn't want, or move the right directory earlier
  in PATH. Two installs can report on different databases.
- **The installer left `~/.local/bin/cci` alone:** it found a regular file
  there, which belongs to another installation, such as
  `pip install --user`. The new install still works from the absolute path
  the installer printed.
- **`Forced include not found` while building:** the dashboard has not been
  built. Build `frontend/` first (see `dashboard` above).
- **`--watch` refused on Windows:** this is deliberate. Task Scheduler does
  not keep processes alive. Use the default 15-minute job.
- **Live mode and the 15-minute job:** only one of them can run, because two
  writers on one database contend. `cci install` and `cci install --watch`
  each remove the other.

## Teams and sign-in

- **`cci team join <link>` says this machine is signed in to another
  server:** one machine holds one sign-in, and its background job pushes
  there. Switching is the user's decision: `cci logout`, then the join again.
- **`cci team invite` (or `join`) fails with `not_found`:** the account
  server is older than the CLI and lacks the join-link routes. Whoever runs
  the server redeploys it from current master; its migrations run on start.
- **`database is locked`:** another `cci` process is writing, usually the
  background job mid-run. Wait a few seconds and repeat the command.

## Upgrade, move, remove

- **Upgrade:** re-run the installer, adding `-s -- --watch` (or
  `--watch` in a clone) if the user had live mode. For a clone install,
  first run `git -C <clone> pull` and `rm -rf <clone>/frontend/dist`;
  otherwise the installer packages the old dashboard. Then run
  `cci doctor`, which asks for `cci init` if the new version brought
  migrations.
- **Custom data location:** run `CC_INSIGHTS_HOME=/path cci install`. That
  pins only the background job. Also export `CC_INSIGHTS_HOME` in the
  user's rc file, or every interactive `cci` command reads `~/.config`
  instead.
- **Costs about double what ccusage reports, on a database built before
  2026-10-06:** older versions counted each Claude response once per content
  block, and some Codex usage twice. Stored rows are never rewritten, so the
  fix needs a rebuild from the logs, and a rebuild loses any history whose
  log files the agents have already deleted. That trade-off is the user's
  call: back up the database file first, and compare `cci stats` before and
  after. `scripts/parity_ccusage.py` in the repo compares per-model tokens
  with ccusage.
- **Old databases missing the cache-write tier in cost:** run `cci backfill`.
  It fills the column from logs still on disk.
- **Uninstall:** `cci install --uninstall` removes only the background job.
  The installer's `--uninstall` also removes the managed install:
  `curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash -s -- --uninstall`.
  On Linux, both remove only the crontab line ending in
  `com.cc-insights`. Both uninstalls keep
  the config directory and its history. Delete that directory only
  when the user explicitly asks for their history to be gone. It cannot be
  rebuilt from logs the agents have already pruned.
