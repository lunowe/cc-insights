Set up CC-Insights for me. It is `cci`, a local-first tracker that reads the logs Claude Code, Codex and opencode already write, and shows how much time I really spend with coding agents, how much of it runs in parallel, which projects take the time, and what it would cost at API list prices. It stores metadata only, never prompt or response text. Repo: https://github.com/lunowe/cc-insights

The full guide is a skill in that repo. Read it before you start, and read each reference file when we get to that topic:
- https://raw.githubusercontent.com/lunowe/cc-insights/master/skills/cc-insights/SKILL.md
- https://raw.githubusercontent.com/lunowe/cc-insights/master/skills/cc-insights/references/sync.md (several machines, signing in)
- https://raw.githubusercontent.com/lunowe/cc-insights/master/skills/cc-insights/references/teams.md (publishing, teams, invites)
- https://raw.githubusercontent.com/lunowe/cc-insights/master/skills/cc-insights/references/troubleshooting.md

If you cannot fetch those files, use this short version:

1. If `cci` is already installed, run `cci doctor`. If it reports `everything is working.`, skip to step 4.
2. Check whether the package is on PyPI: `curl -fsS -o /dev/null https://pypi.org/pypi/cc-insights/json`. On macOS or Linux, if it is on PyPI, run `curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash`. If you get a 404, run `git clone https://github.com/lunowe/cc-insights ~/cc-insights && bash ~/cc-insights/scripts/install.sh`, which also needs pnpm or npm. On Windows, run `pipx install cc-insights`, then `cci install`. The installer finds Python 3.11+, installs `cci`, sets up the config, the database and a background job, then runs `cci doctor`. If it needs a Python or a package installed with sudo, give me the command to run.
3. If the installer says its bin directory is not on my PATH, add the line it prints to the shell rc file it names, and tell me which file you edited. On Linux the background job is one line in my crontab; `cci install` writes it and leaves my other entries alone. If it says `no scheduler found`, this machine has no cron: give me the command to install it, then run `cci install` again. Setup is done when `$SHELL -ilc 'cci doctor'` passes.
4. Run `cci stats` and summarize what you see. Active time counts only spans without a gap of more than 5 minutes, so it is far lower than how long sessions sat open. `cci cost` (after `cci price sync`) gives a list-price equivalent, not a bill. Then start `cci serve` in the background so the dashboard opens at http://localhost:8787.
5. Ask me whether I want to sync several machines (`cci login --server <url>`, then `cci sync push` and `cci sync pull`) or share with a team (`cci publish`, `cci team join <code>`, `cci team new`). Each of these needs an account server URL, which I will give you. There is no public default.

Rules while you do this:
- `cci doctor` decides whether something works. Every line that is not `ok` names the command that fixes it.
- Never delete or move `~/.config/cc-insights` (`%APPDATA%\cc-insights` on Windows), and never change `host_id` in its `config.toml`. The database there holds history the agents have already deleted from their logs. Don't print `credentials.toml`; it holds a token.
- Run exactly one background job per machine. Don't start `cci watch` while a job is installed; use `cci serve` to look at the data.
- `cci login` blocks until I approve a code in my browser. Start it with `nohup cci login --server <url> > /tmp/cci-login.log 2>&1 &`, give me the URL and the code from that file, then check the file every few seconds until it says `signed in as`.
- Before anything leaves this machine for other people, run `cci publish --dry-run`, show me the report, and tell me that anyone on that server with access to those repos on GitHub can read what I publish. Run `cci publish --yes` only after I say yes. If you mint a team join code, give it only to me, and tell me it is shown only once and should be sent privately.

Once everything works, offer to save the skill folder (https://github.com/lunowe/cc-insights/tree/master/skills/cc-insights) into your skills directory, so it is available next time I ask about cci.
