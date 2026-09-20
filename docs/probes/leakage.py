"""What would actually leak, measured on a real corpus.

THIS FILE IS THE EVIDENCE for docs/REDACTION.md. Run it against your own
database before trusting any claim in that document:

    python docs/probes/leakage.py [~/.config/cc-insights/cc-insights.db]

It is read-only. It prints aggregates and redacted samples, never a full path
that is not already yours.

Three questions:

  1. Does publishing `project_id` publish the path? `project_id` is
     `sha256(root_path)[:32]`, and a colleague can hash a guess. If guessed
     paths match real ids, the hash is not redaction -- it is the path with an
     extra step, and every design that "just ships the ids" is broken.
  2. How much work sits behind a git remote? The remote is the only thing that
     can answer "who may see this" without a new permission system, so
     everything else has no access boundary to inherit.
  3. What is in the fields themselves -- usernames, client names, branch names?
"""

from __future__ import annotations

import collections
import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

ID_LEN = 32
SEP = "\x1f"


def make_id(*parts: str | None) -> str:
    joined = SEP.join("\x00" if p is None else p for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:ID_LEN]


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------------------
# 1. the confirmation attack
# ---------------------------------------------------------------------------


def guessable_paths(conn: sqlite3.Connection) -> list[str]:
    """Paths a colleague could plausibly type, built only from public knowledge.

    The ingredients are: your username (in every git commit you have ever
    pushed), a handful of conventional parent directories, and the repo names
    from the remotes -- which anyone with repo access already has. Nothing here
    requires seeing the database.
    """
    user = os.environ.get("USER") or Path.home().name
    homes = [f"/Users/{user}", f"/home/{user}", rf"C:\Users\{user}"]
    parents = ["Coding", "code", "src", "dev", "Projects", "work", "repos", "git"]

    repos = {
        r[0]
        for r in conn.execute(
            "SELECT repo FROM project_group WHERE repo IS NOT NULL AND repo <> ''"
        )
    }

    out = []
    for home in homes:
        sep = "\\" if home.startswith("C:") else "/"
        out.append(home)
        for parent in parents:
            out.append(f"{home}{sep}{parent}")
            for repo in repos:
                out.append(f"{home}{sep}{parent}{sep}{repo}")
                # The worktree layouts grouping.py already knows about.
                out.append(f"{home}{sep}{parent}{sep}{repo}{sep}.claude{sep}worktrees")
    return out


def confirmation_attack(conn: sqlite3.Connection) -> None:
    rule("1. Does publishing project_id publish the path?")
    real = {r[0]: r[1] for r in conn.execute("SELECT project_id, root_path FROM project")}
    guesses = guessable_paths(conn)
    hit = {make_id(g): g for g in guesses}

    found = [(pid, path) for pid, path in real.items() if pid in hit]
    print(f"  project rows            {len(real)}")
    print(f"  paths guessed           {len(guesses):,} (username + 8 dirs + known repo names)")
    print(f"  ids confirmed by guess  {len(found)}  <-- these paths are NOT hidden by hashing")
    if found:
        print("\n  confirmed (the guess reproduced the published id exactly):")
        for pid, path in sorted(found, key=lambda t: t[1])[:12]:
            print(f"    {pid}  {path}")
    share = len(found) / len(real) if real else 0
    print(f"\n  => {share:.0%} of this corpus is recoverable from the id alone.")
    print("     A salt would break it, but a per-machine salt also breaks the")
    print("     cross-machine identity the whole schema is built on.")


# ---------------------------------------------------------------------------
# 2. what has an access boundary to inherit
# ---------------------------------------------------------------------------


def _H(ms: float) -> str:
    return f"{ms / 3_600_000:,.1f} h"


def repo_backed(conn: sqlite3.Connection) -> None:
    rule("2. How much work sits behind a git remote?")
    rows = conn.execute(
        """SELECT p.project_id, p.root_path, g.remote_url, g.forge,
                  coalesce(sum(sp.ended_at - sp.started_at), 0) AS ms
           FROM project p
           LEFT JOIN project_group g ON g.group_id = p.group_id
           LEFT JOIN session s  ON s.project_id = p.project_id
           LEFT JOIN span    sp ON sp.session_id = s.id
           GROUP BY p.project_id, p.root_path, g.remote_url, g.forge"""
    ).fetchall()

    with_remote = [r for r in rows if r[2]]
    without = [r for r in rows if not r[2]]
    ms_with = sum(r[4] for r in with_remote)
    ms_without = sum(r[4] for r in without)
    total = ms_with + ms_without

    print(f"  behind a remote     {len(with_remote):>3} projects   {_H(ms_with):>10}"
          f"   {ms_with / total:>4.0%}" if total else "")
    print(f"  no remote at all    {len(without):>3} projects   {_H(ms_without):>10}"
          f"   {ms_without / total:>4.0%}" if total else "")
    print("\n  Work with no remote has nothing to derive permission from. It is")
    print("  not 'redact harder' -- there is no question 'may Bob see this?'")
    print("  that anything in the data can answer.\n")

    forges = collections.Counter(r[3] or "(none)" for r in with_remote)
    print(f"  forges: {dict(forges)}")

    print("\n  the largest remote-less items (name shown, path withheld):")
    for r in sorted(without, key=lambda r: -r[4])[:8]:
        name = Path(r[1].replace("\\", "/")).name or r[1]
        print(f"    {_H(r[4]):>10}   {name}")


# ---------------------------------------------------------------------------
# 3. what the fields themselves say
# ---------------------------------------------------------------------------


_SECRETISH = re.compile(
    r"client|kunde|nda|secret|private|confidential|internal|restricted|patent"
    r"|acquisition|merger|layoff|salary|payroll|invoice",
    re.IGNORECASE,
)


def field_contents(conn: sqlite3.Connection) -> None:
    rule("3. What is actually in the fields?")
    user = os.environ.get("USER") or Path.home().name

    paths = [r[0] for r in conn.execute("SELECT root_path FROM project")]
    cwds = [r[0] for r in conn.execute("SELECT cwd FROM session WHERE cwd IS NOT NULL")]
    branches = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT git_branch FROM session WHERE git_branch IS NOT NULL"
        )
    ]
    hostnames = [r[0] for r in conn.execute("SELECT hostname FROM host")]

    def pct(hits: int, n: int) -> str:
        return f"{hits}/{n} ({hits / n:.0%})" if n else "0/0"

    print(f"  root_path carrying your username        {pct(sum(user in p for p in paths), len(paths))}")
    print(f"  session.cwd carrying your username      {pct(sum(user in c for c in cwds), len(cwds))}")
    print(f"  hostname carrying your username         {pct(sum(user.lower() in h.lower() for h in hostnames), len(hostnames))}")
    print(f"  distinct branch names                   {len(branches)}")

    flagged = [b for b in branches if _SECRETISH.search(b)]
    flagged += [Path(p.replace('\\', '/')).name for p in paths if _SECRETISH.search(p)]
    print(f"  values matching a 'sensitive-sounding' word list   {len(flagged)}")
    for f in sorted(set(flagged))[:10]:
        print(f"    {f}")
    print("\n  The word list proves the shape of the risk, not its extent: it")
    print("  cannot know which of YOUR names are the sensitive ones. That is")
    print("  exactly why the design cannot be a denylist.")


def main() -> int:
    default = Path.home() / ".config" / "cc-insights" / "cc-insights.db"
    db_path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else default
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    print(f"corpus: {db_path}")
    try:
        confirmation_attack(conn)
        repo_backed(conn)
        field_contents(conn)
    finally:
        conn.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
