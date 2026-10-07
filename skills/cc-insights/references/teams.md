# Teams

Everyone on a team signs in to the same server. A join link names that
server and signs in by itself; every other command here needs sign-in first
([sync.md](sync.md)).

## What crosses the boundary

The team store is a different store from the personal one, and it holds a
**redacted projection**:

- **Sent:** the user's actor name (their GitHub login), repo id (a hash of
  the git remote), remote URL, branch, source, timings and counts.
- **Never sent:** paths, hostnames, project ids, prompt or response text, raw
  events.
- **Withheld:** work in a directory with no git remote. Nothing in that data
  can say who may see it, so it is sent only as a total of hours. The team
  view shows that total as withheld time, so the user should expect it and
  not treat it as a bug.

**Who can read published rows:** anyone on the same server whose GitHub
access to the repo the server has verified can read every published row for
that repo, with or without a team. Teams add people *through* a roster (see
below). Tell the user this before their first publish. On a private
instance it means their teammates, and on a shared instance it means anyone
there with access to that repo.

Publishing is manual. The background job never publishes, so the team view
only shows what has been published. Run `cci publish` again to update it.
After the first confirmed publish on a machine, later runs don't prompt.

## Publishing

1. `cci privacy` shows what would and would not cross, and sends nothing.
   `--show` adds sample rows. Walk the user through it.
2. `cci publish --dry-run` prints the SENDING and WITHHELD report and sends
   nothing. Show the report to the user.
3. Only after the user explicitly says yes, run `cci publish --yes`. Without
   `--yes`, the first publish on a machine asks the user to type `publish`
   on stdin. That confirmation belongs to the user, and `--yes` records that
   they gave it.

If publish prints `REFUSING TO PUBLISH`, it failed its own privacy audit.
Relay the reason and stop. The refusal is the product working as designed.

## Running a team (admin)

```bash
cci team new <name>                 # you become its admin
cci team repos --ids                # repos in your scope
cci team share <repo>               # put a repo on the team's roster
cci team share --no-branches <repo> # ...with branch names hidden
cci team branches off <repo>        # hide branch names on a repo already shared
cci team invite                     # mint a join link: single-use, 3 days
cci team invite --uses 3 --expires-in 1w --note "backend team"   # caps: 50 uses, 30 days
cci team invites                    # outstanding links and who redeemed what
cci team revoke <invite-id>
cci team remove <who>
```

These are the things that surprise people. Tell the user before they act:

- **A join link is a password, and it is shown exactly once.** `cci team
  invite` prints `https://<server>/join/<code>`; the code is stored hashed
  and can never be displayed again. Give the link to the user only, and
  suggest they send it in a direct message, not a channel. If it is lost,
  mint a new one and revoke the old one. The colleague needs nothing else:
  the link names the server, the installer accepts it (`--join <link>`), and
  opened in a browser it shows both commands.
- **Branch names are published by default.** They are free text and can say
  more than intended, for example `feat/acme-layoffs`. Hide them on sensitive
  repos *before* inviting anyone. **Running `cci team share` again turns them
  back on** unless `--no-branches` is passed again. When re-sharing a repo,
  check `cci team repos` first and repeat the flag.
- **Sharing gives the team what *you* can see.** That is every row in the
  repo if the server verified your GitHub access to it (this needs the
  server's `repo` scope), or only your own published rows if it did not. The
  output of `cci team share` says which one happened.
- **Joining widens nothing.** A new member sees what the roster already
  shares, and existing members see nothing new of theirs. Within a team,
  only `cci team share` changes who can read what.
- **`unshare` stops the next read; it cannot un-read.** Whatever the team
  already saw stays seen.
- **Removing a member does not kill their link.** After
  `cci team remove`, run `cci team invites`. A link with seats left can
  readmit the person, so revoke it.
- **An invalid link and an expired one return the same error**, on purpose,
  and the browser page looks the same for both. When a colleague says the
  link doesn't work, check `cci team invites`.

## Joining a team (member)

The admin sends a join link, `https://<server>/join/<code>`. It names the
server, so nothing else is needed:

```bash
cci team join <link>       # signs in to the link's server if needed, then redeems
cci publish --dry-run      # then publish as described above
```

`team join <link>` runs the same browser sign-in as `cci login` when the
machine is not signed in yet, and blocks until the user approves it. Run it
the way sync.md step 2 runs `cci login` -- in the background, output to a
file, relaying the code -- and poll for `joined <team>` instead of
`signed in as`. On a
machine with nothing installed, the installer does all of it:
`curl -fsSL https://raw.githubusercontent.com/lunowe/cc-insights/master/scripts/install.sh | bash -s -- --join <link>`.
A machine signed in to a different server refuses the link; `cci logout`
first switches it. Redeeming is the consent, so the user runs or approves it.
Treat the link as a password: keep it out of logs and messages to anyone else.

## Reading

```bash
cci team             # repos in scope and the scope-aware summary
cci team sessions    # published sessions you can see (--limit)
cci team actors      # per-person totals
cci team daily       # active time per UTC day
cci team list        # your teams and your role on each
cci team members     # who is on the team, and who let them in
```

On several teams, commands that act on a team need `--team <name>`. On
exactly one team, that team is used automatically.
