"""What may leave this machine, and in what shape. See docs/REDACTION.md.

`sync.py` moves everything between one person's machines, paths included, and
that is right -- it is all the same disk. This module is the other pipe: the
one a *second person* can see through.

Two rules carry the whole design.

**Path-derived identity cannot cross.** `project_id = sha256(root_path)[:32]`,
so publishing the id publishes the path to anyone who can guess it -- and on
the author's corpus, 555 guesses built from a username, eight conventional
directory names and the repo names in the remotes recovered 20% of the project
ids outright. Hashing is not redaction; it is the path with an extra step, and
salting would break the cross-machine identity the schema is built on. So
published rows are keyed on the normalized git remote, which is already public
on the other side of the boundary.

**Redaction happens here, not at query time.** The shared database never
receives a path. Filtering on the way out fails the first time anything goes
wrong -- one API bug, one backup, one `psql` session -- and there is no
un-leaking. This is the same discipline that has kept prompt text out of the
schema: enforced where the row is written, not where it is read.

The boundary itself is repo access: a row may be published only if it belongs
to a repo, and only to people who can already see that repo. Work with no
remote has nothing to derive permission from, so it stays local -- withheld
and *counted*, because silently dropping it would misreport totals.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from cc_insights import grouping
from cc_insights.ids import make_id

PUBLIC = "public"      # safe for anyone who can already see the repo
PRIVATE = "private"    # never leaves this machine
DERIVED = "derived"    # replaced by something computed; the original stays here


@dataclass(frozen=True)
class Field:
    table: str
    column: str
    verdict: str
    why: str


def _f(table: str, spec: dict[str, tuple[str, str]]) -> list[Field]:
    return [Field(table, col, v, why) for col, (v, why) in spec.items()]


#: Every column in the schema, classified. A test fails when a migration adds
#: one that is not here -- see `unclassified`.
#:
#: A denylist would be the wrong shape: it lists the leaks someone thought of,
#: and the next migration adds one that is not on it. This is closed by
#: default, so a new column is withheld until a human has looked at it.
FIELDS: tuple[Field, ...] = tuple(
    _f("host", {
        "host_id": (DERIVED, "a UUID, but it silently links one person's machines; "
                             "a team view should name the actor on purpose"),
        "hostname": (PRIVATE, "commonly carries a person's name"),
        "os": (PUBLIC, "platform only, no identity"),
        "first_seen": (PRIVATE, "says when this machine was set up, not about any repo"),
        "last_seen": (PRIVATE, "says when this machine was last used, which is "
                               "attendance data, not agent-time data"),
    })
    + _f("project", {
        "project_id": (PRIVATE, "sha256(root_path) -- publishing it publishes the path"),
        "root_path": (PRIVATE, "the leak itself: username, client names, worktree names"),
        "name": (PRIVATE, "the last path segment, so the most sensitive part of it"),
        "group_id": (DERIVED, "replaced by repo_id, keyed on the remote instead"),
        "group_pinned": (PRIVATE, "a local organising choice, meaningless to anyone else"),
    })
    + _f("project_group", {
        "group_id": (DERIVED, "replaced by repo_id"),
        "name": (PUBLIC, "the repo name, which repo access already reveals"),
        "origin": (PUBLIC, "which rule matched; says nothing about the disk"),
        "match_key": (PRIVATE, "a path for rules 2-4; only the remote rule is safe"),
        "remote_url": (PUBLIC, "credential-stripped by normalize_remote, and the "
                               "thing the viewer's access is derived from"),
        "forge": (PUBLIC, "implied by remote_url"),
        "owner": (PUBLIC, "implied by remote_url"),
        "repo": (PUBLIC, "implied by remote_url"),
        "web_url": (PUBLIC, "implied by remote_url"),
        "created_at": (PRIVATE, "when detection first ran here, a fact about this machine"),
        "updated_at": (PRIVATE, "when detection last ran here, a fact about this machine"),
    })
    + _f("project_probe", {
        "project_id": (PRIVATE, "path-derived"),
        "host_id": (PRIVATE, "identifies a machine"),
        "git_remote": (PRIVATE, "the raw, unnormalized remote may still carry a username"),
        "git_common_dir": (PRIVATE, "a path"),
        "path_exists": (PRIVATE, "a description of someone else's disk"),
        "detected_at": (PRIVATE, "when this machine last looked at its own disk"),
    })
    + _f("session", {
        "id": (PUBLIC, "hashes a log UUID, not a path: unguessable and stable"),
        "native_id": (PRIVATE, "the vendor's own id; no reason to export it"),
        "source": (PUBLIC, "which agent; the point of the comparison"),
        "host_id": (DERIVED, "replaced by actor"),
        "project_id": (DERIVED, "replaced by repo_id"),
        "cwd": (PRIVATE, "a path -- 100% of them carry the username on this corpus"),
        "git_branch": (PUBLIC, "repo access already shows the branch list; see "
                               "docs/REDACTION.md §5 on the opt-out this still needs"),
        "cli_version": (PRIVATE, "a fingerprint that answers no question anyone asked"),
        "started_at": (PUBLIC, "the measurement"),
        "ended_at": (PUBLIC, "the measurement"),
        "event_count": (PUBLIC, "the measurement"),
        "active_ms": (PUBLIC, "the measurement"),
    })
    + _f("thread", {
        "id": (PUBLIC, "hashes a log UUID"),
        "native_id": (PRIVATE, "the vendor's own id"),
        "session_id": (PUBLIC, "hashes a log UUID"),
        "parent_thread_id": (PUBLIC, "structure, not content"),
        "is_subagent": (DERIVED, "folded into thread_role"),
        "agent_name": (PRIVATE, "a subagent name can be a project or client codename"),
        "started_at": (PUBLIC, "the measurement"),
        "ended_at": (PUBLIC, "the measurement"),
        "event_count": (PUBLIC, "the measurement"),
        "active_ms": (PUBLIC, "the measurement"),
    })
    + _f("event", {
        # Events are not published at all: 187k rows whose analytical value is
        # already carried by spans, and tool_name/model is a finer-grained
        # behavioural picture of a person than a team view has any business
        # holding. Classified anyway, so the coverage guard stays honest.
        "id": (PRIVATE, "events are not published; spans carry the time math"),
        "session_id": (PRIVATE, "events are not published"),
        "thread_id": (PRIVATE, "events are not published"),
        "native_event_id": (PRIVATE, "events are not published"),
        "ts": (PRIVATE, "per-event timing is a finer picture of a person than "
                        "a team view needs"),
        "ordinal": (PRIVATE, "events are not published"),
        "kind": (PRIVATE, "events are not published"),
        "model": (PRIVATE, "events are not published"),
        "tool_name": (PRIVATE, "which tools someone leans on is about them, not the repo"),
        "tool_use_id": (PRIVATE, "events are not published"),
        "input_tokens": (PRIVATE, "events are not published"),
        "output_tokens": (PRIVATE, "events are not published"),
        "cache_read_tokens": (PRIVATE, "events are not published"),
        "cache_write_tokens": (PRIVATE, "events are not published"),
    })
    + _f("span", {
        "id": (PUBLIC, "derived from a thread id and a timestamp, not a path"),
        "session_id": (PUBLIC, "hashes a log UUID"),
        "thread_id": (PUBLIC, "hashes a log UUID"),
        "started_at": (PUBLIC, "the measurement"),
        "ended_at": (PUBLIC, "the measurement"),
        "event_count": (PUBLIC, "the measurement"),
        "attended": (DERIVED, "folded into thread_role"),
    })
    + _f("ingest_file", {
        "host_id": (PRIVATE, "local bookkeeping; never synced at all"),
        "path": (PRIVATE, "a path"),
        "source": (PRIVATE, "local bookkeeping"),
        "size_bytes": (PRIVATE, "local bookkeeping"),
        "mtime_ms": (PRIVATE, "local bookkeeping"),
        "bytes_read": (PRIVATE, "local bookkeeping"),
        "lines_read": (PRIVATE, "local bookkeeping"),
        "last_ingest": (PRIVATE, "local bookkeeping"),
    })
    + _f("schema_migrations", {
        "version": (PRIVATE, "about this database's schema, not about any repo"),
        "applied_at": (PRIVATE, "when this database was migrated, not about any repo"),
    })
    # v1's cost tables. Classified here so `cci privacy` is usable on a merged
    # database, and classified CLOSED because that is what the default is for:
    # per-event cost is a minute-by-minute behavioural profile of a person --
    # which model they reached for, how often they burned cache -- and a team
    # view wants cost per repo, which is an aggregate this machine can compute
    # before publishing rather than a stream of rows it has to ship.
    #
    # REVISIT AT MERGE: the aggregate itself is a reasonable thing to publish
    # and there is no field for it yet. Whoever adds one should add it here.
    + _f("model_price", {
        "model": (PUBLIC, "a vendor's public price list; says nothing about anyone"),
        "input_mtok": (PUBLIC, "public price list"),
        "output_mtok": (PUBLIC, "public price list"),
        "cache_read_mtok": (PUBLIC, "public price list"),
        "cache_write_mtok": (PUBLIC, "public price list"),
        "currency": (PUBLIC, "public price list"),
        "effective_from": (PUBLIC, "public price list"),
        "origin": (PRIVATE, "whether a human overrode a rate here is local bookkeeping"),
        "matched_id": (PRIVATE, "local bookkeeping"),
        "note": (PRIVATE, "free text a human typed on this machine"),
        "updated_at": (PRIVATE, "about this database"),
    })
    + _f("event_cost", {
        "event_id": (PRIVATE, "per-event rows are not published; see the note above"),
        "session_id": (PRIVATE, "per-event rows are not published"),
        "thread_id": (PRIVATE, "per-event rows are not published"),
        "ts": (PRIVATE, "per-event timing is a finer picture of a person than a "
                        "team view needs"),
        "model": (PRIVATE, "which model someone reaches for is about them, not the repo"),
        "input_nano": (PRIVATE, "publish cost per repo, not per event"),
        "output_nano": (PRIVATE, "publish cost per repo, not per event"),
        "cache_read_nano": (PRIVATE, "publish cost per repo, not per event"),
        "cache_write_nano": (PRIVATE, "publish cost per repo, not per event"),
        "price_from": (PRIVATE, "per-event rows are not published"),
        "attributed": (PRIVATE, "per-event rows are not published"),
    })
    + _f("event_unpriced", {
        "event_id": (PRIVATE, "per-event rows are not published"),
        "session_id": (PRIVATE, "per-event rows are not published"),
        "thread_id": (PRIVATE, "per-event rows are not published"),
        "ts": (PRIVATE, "per-event rows are not published"),
        "model": (PRIVATE, "per-event rows are not published"),
        "tokens": (PRIVATE, "per-event rows are not published"),
        "reason": (PRIVATE, "per-event rows are not published"),
        "attributed": (PRIVATE, "per-event rows are not published"),
    })
)

_BY_COLUMN = {(f.table, f.column): f for f in FIELDS}

#: Tables whose rows are never published, whatever their columns say.
WITHHELD_TABLES = frozenset({"project_probe", "event", "ingest_file", "schema_migrations"})


def verdict(table: str, column: str) -> Field | None:
    return _BY_COLUMN.get((table, column))


def unclassified(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Schema columns nobody has ruled on. Must be empty; a test enforces it.

    This is the mechanism that makes "closed by default" real. A migration that
    adds a column is a migration that has to answer, in writing, whether it may
    cross the boundary -- at the moment the author still remembers what it is
    for, rather than in an incident review later.
    """
    missing: list[tuple[str, str]] = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        for row in conn.execute(f"PRAGMA table_info({name})"):
            if (name, row[1]) not in _BY_COLUMN:
                missing.append((name, row[1]))
    return sorted(missing)


# --------------------------------------------------------------------------
# published identity
# --------------------------------------------------------------------------


def repo_id(normalized_remote: str) -> str:
    """The published key for one repository.

    Hashed for a stable, fixed-width key -- NOT for secrecy. The input is the
    credential-stripped remote, which everyone on the far side of the boundary
    already has, so there is nothing for the confirmation attack of
    docs/REDACTION.md §0 to confirm. That is the difference between this and
    `project_id`, and it is the whole reason publication re-keys.
    """
    return make_id("repo", normalized_remote)


# --------------------------------------------------------------------------
# the projection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    repo_id: str
    remote_url: str
    forge: str | None
    owner: str | None
    repo: str | None
    web_url: str | None
    name: str


@dataclass(frozen=True)
class Session:
    session_id: str
    repo_id: str
    actor: str
    source: str
    git_branch: str | None
    started_at: int
    ended_at: int
    active_ms: int
    event_count: int


@dataclass(frozen=True)
class Span:
    span_id: str
    session_id: str
    thread_role: str
    started_at: int
    ended_at: int
    event_count: int


#: The three buckets that partition active time, as `stats.py` computes them.
#: Published as a label so the partition survives aggregation.
HUMAN = "human"
AUTONOMOUS = "autonomous"
UNATTENDED = "unattended_root"


def thread_role(is_subagent: int, attended: int | None) -> str:
    if is_subagent:
        return AUTONOMOUS
    return HUMAN if attended == 1 else UNATTENDED


@dataclass
class Publication:
    """Everything that would cross the boundary, plus what did not and why."""

    actor: str = ""
    repos: list[Repo] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    withheld_ms: int = 0
    withheld_projects: int = 0
    published_ms: int = 0

    @property
    def total_ms(self) -> int:
        return self.published_ms + self.withheld_ms


def _publishable_repos(conn: sqlite3.Connection) -> dict[str, Repo]:
    """group_id -> Repo, for every group whose remote can answer "who may see this".

    The remote is re-normalized here rather than trusted from the row. It is
    cheap, and `normalize_remote` is what strips credentials -- real remotes on
    this corpus carry a username, and a stored value that predates a fix to
    that function must not be the thing that ships.
    """
    out: dict[str, Repo] = {}
    for r in conn.execute(
        """SELECT group_id, name, remote_url, forge, owner, repo, web_url
           FROM project_group WHERE remote_url IS NOT NULL AND remote_url <> ''"""
    ):
        normalized = grouping.normalize_remote(r["remote_url"])
        if not normalized:
            continue
        forge = grouping.parse_forge(normalized)
        out[r["group_id"]] = Repo(
            repo_id=repo_id(normalized),
            remote_url=normalized,
            forge=forge.forge,
            owner=forge.owner,
            repo=forge.repo,
            web_url=forge.web_url,
            name=r["name"],
        )
    return out


def publication(conn: sqlite3.Connection, actor: str, *, host_id: str | None = None) -> Publication:
    """Build everything that may cross the boundary, for one actor.

    `actor` comes from auth, which does not exist yet; this module has no
    opinion about where it came from, only that `host_id` must not be it.
    `host_id` scopes the export to one machine's own sessions, which is what a
    real publish would do; None exports whatever is local, so the report can be
    run against a database that has already pulled other machines in.
    """
    repos = _publishable_repos(conn)
    pub = Publication(actor=actor, repos=sorted(repos.values(), key=lambda r: r.repo_id))

    where = "WHERE s.host_id = ?" if host_id else ""
    params = (host_id,) if host_id else ()

    session_repo: dict[str, str] = {}
    for r in conn.execute(
        f"""SELECT s.id, s.source, s.git_branch, s.started_at, s.ended_at,
                   s.active_ms, s.event_count, p.group_id
            FROM session s LEFT JOIN project p ON p.project_id = s.project_id
            {where}""",
        params,
    ):
        repo = repos.get(r["group_id"]) if r["group_id"] else None
        if repo is None:
            continue
        session_repo[r["id"]] = repo.repo_id
        pub.sessions.append(
            Session(
                session_id=r["id"],
                repo_id=repo.repo_id,
                actor=actor,
                source=r["source"],
                git_branch=r["git_branch"],
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                active_ms=r["active_ms"],
                event_count=r["event_count"],
            )
        )

    for r in conn.execute(
        f"""SELECT sp.id, sp.session_id, sp.started_at, sp.ended_at, sp.event_count,
                   sp.attended, t.is_subagent
            FROM span sp
            JOIN thread t  ON t.id = sp.thread_id
            JOIN session s ON s.id = sp.session_id
            {where}""",
        params,
    ):
        if r["session_id"] not in session_repo:
            continue
        pub.spans.append(
            Span(
                span_id=r["id"],
                session_id=r["session_id"],
                thread_role=thread_role(r["is_subagent"], r["attended"]),
                started_at=r["started_at"],
                ended_at=r["ended_at"],
                event_count=r["event_count"],
            )
        )

    pub.published_ms = sum(s.ended_at - s.started_at for s in pub.spans)
    pub.withheld_ms, pub.withheld_projects = _withheld(conn, repos, host_id, pub.published_ms)
    return pub


def _withheld(
    conn: sqlite3.Connection,
    repos: dict[str, Repo],
    host_id: str | None,
    published_ms: int,
) -> tuple[int, int]:
    """Active time and project count that cannot be published.

    Counted, never silently dropped: a team dashboard that quietly omits 8% of
    someone's time is not private, it is wrong, and the person reading it has
    no way to tell the difference.

    Withheld time is TOTAL minus published, not a sum over the projects that
    failed the test. Summing the failures looks equivalent and is not: a
    session whose `cwd` never resolved has no project row at all, so it
    belongs to neither set and would vanish from both -- which is the exact
    silent undercount this function exists to prevent. Subtracting makes the
    two numbers partition by construction.
    """
    where = "WHERE s.host_id = ?" if host_id else ""
    params = (host_id,) if host_id else ()
    total = conn.execute(
        f"""SELECT coalesce(sum(sp.ended_at - sp.started_at), 0)
            FROM span sp JOIN session s ON s.id = sp.session_id {where}""",
        params,
    ).fetchone()[0]

    loose = conn.execute(
        "SELECT count(*) FROM project WHERE group_id IS NULL OR group_id NOT IN (%s)"
        % (", ".join("?" for _ in repos) or "NULL"),
        tuple(repos),
    ).fetchone()[0]
    return total - published_ms, loose


# --------------------------------------------------------------------------
# the audit
# --------------------------------------------------------------------------


def _values(pub: Publication) -> Iterable[tuple[str, str, object]]:
    for kind, rows in (("repo", pub.repos), ("session", pub.sessions), ("span", pub.spans)):
        for row in rows:
            for name, value in vars(row).items():
                yield kind, name, value


@dataclass(frozen=True)
class Secrets:
    """What this machine knows about itself, split by how it must be matched.

    This is narrow on purpose, and it took three tries against a real corpus to
    get there. Matching every path *segment* as a substring reported that
    `https://github.com/northwind-labs/atlas-chat` leaks the
    local directory `ich`. Matching whole segments instead still reported that
    `source = 'codex'` leaks `~/.codex`, and that a branch called
    `t3code/frontend-chat-performance` leaks a directory called `frontend`.
    160 findings, every one of them wrong.

    The lesson is the one docs/REDACTION.md §4 already makes about denylists,
    arriving from the other side: **the tool cannot tell which of your words are
    the secret ones.** So the audit asserts only what it can prove, and the
    classification table -- reviewed by a human -- is what carries the judgement.

    `paths` are full local paths, the home directory, and every path-derived
    id: a leak wherever they appear, as a substring, no argument. `tokens` are
    the few values that identify a person or a machine rather than a word that
    happens to also be a directory name, and they match as whole tokens, and
    only when they are not already public on the far side of the boundary.
    """

    paths: tuple[str, ...] = ()
    identity: tuple[str, ...] = ()
    names: tuple[str, ...] = ()


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

#: Structural noise every path and every URL is made of. These are matched as
#: whole tokens, so leaving them in produces confident nonsense -- `_git` in an
#: Azure remote against a `.git` directory on disk -- and they can never
#: identify anything on their own.
_STRUCTURAL = frozenset(
    "http https www com org net io dev git github gitlab src lib app main master "
    "users home root var tmp opt usr etc bin new old test tests doc docs "
    "local localhost".split()
)


def tokenize(value: str) -> set[str]:
    """Casefolded alphanumeric runs. One tokenizer for both sides of the audit.

    Whole tokens, never substrings. A substring audit reports that
    `https://github.com/northwind-labs/atlas-chat` leaks the
    local directory `ich`, which it does not -- and 171 findings like that on
    the first real run is not a careful audit, it is one nobody will read.
    """
    return {m.group(0).casefold() for m in _TOKEN_RE.finditer(value)}


def public_tokens(pub: Publication) -> set[str]:
    """Values a viewer already has, or that this publication names on purpose.

    The actor belongs here: naming the person IS the job of that field, so
    auditing it against "things that identify a person" flags the one value
    that is deliberate. Once the actor is published, their name appearing
    elsewhere discloses nothing new either.
    """
    out: set[str] = set(_STRUCTURAL)
    out |= tokenize(pub.actor)
    for repo in pub.repos:
        for value in (repo.owner, repo.repo, repo.name, repo.remote_url, repo.web_url):
            if value:
                out |= tokenize(value)
    return out


@dataclass
class Audit:
    """Two severities, because two very different kinds of claim.

    `leaks` are provable: a full local path, or a `project_id` -- which IS
    sha256 of a path -- appearing in a published field, or this machine's
    username or hostname as a whole token. Any one of them is a defect. This
    list must be empty and publication must refuse while it is not.

    `warnings` are a heuristic, and they exist because the honest version of
    this check has an irreducible residue. A withheld project's directory name
    is worth noticing in published text -- that is where a client name lives --
    but directory names are ordinary words and ordinary words collide. On the
    author's corpus, `~/Coding/CC-Insights/frontend` is withheld (the tool is
    not pushed yet) and the published branch `t3code/frontend-chat-performance`
    contains `frontend`. Nothing leaked; two unrelated things are both called
    frontend.

    Reporting that as a blocker would be the mistake docs/REDACTION.md §4 warns
    about from the other direction: a check nobody can satisfy is a check
    everybody learns to skip.
    """

    leaks: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.leaks


def audit(pub: Publication, secrets: Secrets) -> Audit:
    """Check what the projection actually produced, field by field.

    The projection is built one field at a time, so in principle nothing can
    slip through. This asserts it anyway, because "in principle" is exactly how
    the confirmation attack in docs/REDACTION.md §0 survived into a design.
    """
    exempt = public_tokens(pub)
    identity = {s.casefold() for s in secrets.identity if len(s) >= 3} - exempt
    names = {s.casefold() for s in secrets.names if len(s) >= 3} - exempt - identity
    path_needles = [s for s in secrets.paths if len(s) >= 3]

    out = Audit()
    for kind, name, value in _values(pub):
        if not isinstance(value, str):
            continue
        low = value.casefold()
        for needle in path_needles:
            if needle.casefold() in low:
                out.leaks.append(
                    f"{kind}.{name} leaks the local path or path-derived id "
                    f"{needle!r}: {value!r}"
                )
        seen = tokenize(value)
        for token in sorted(seen & identity):
            out.leaks.append(f"{kind}.{name} leaks this machine's {token!r}: {value!r}")
        for token in sorted(seen & names):
            out.warnings.append(
                f"{kind}.{name} contains {token!r}, also the directory name of a "
                f"withheld project: {value!r}"
            )
    return out


def local_secrets(conn: sqlite3.Connection) -> Secrets:
    """Everything local this machine can prove is local."""
    import getpass
    import os
    from pathlib import Path

    from cc_insights import paths as pathmod

    full: list[str] = [str(Path.home())]
    full += [r[0] for r in conn.execute("SELECT root_path FROM project")]
    full += [r[0] for r in conn.execute("SELECT DISTINCT cwd FROM session WHERE cwd IS NOT NULL")]
    # The sharpest check in the file: project_id IS sha256(root_path), so one
    # of these reaching a published field is the confirmation attack of
    # docs/REDACTION.md §0 happening, not a heuristic about it.
    full += [r[0] for r in conn.execute("SELECT project_id FROM project")]

    identity: list[str] = []
    try:
        identity.append(getpass.getuser())
    except Exception:                                   # pragma: no cover
        pass
    identity.append(os.environ.get("USER") or "")
    identity.append(Path.home().name)
    identity += [r[0] for r in conn.execute("SELECT hostname FROM host")]
    # Tokens as well as whole values: `alice-macbook` splits into `alice` and
    # `macbook`, and a whole-token audit would never have matched either.
    for value in list(identity):
        identity += tokenize(value)

    names: list[str] = []
    # Leaf segments of WITHHELD projects only -- the directory a piece of work
    # is named after, which is where a client or unreleased product name lives.
    #
    # Two exclusions, both load-bearing. Interior segments (`Users`, `Coding`,
    # `Documents`) are generic words that collide with real content and prove
    # nothing. And a leaf inside a *publishable* repo is not secret either: it
    # is a directory in a repo the viewer can already check out, so flagging
    # `frontend` in the branch `t3code/frontend-chat-performance` says only
    # that the repo has a frontend/ directory, which the viewer knew.
    publishable = set(_publishable_repos(conn))
    for root_path, group_id in conn.execute("SELECT root_path, group_id FROM project"):
        if group_id in publishable:
            continue
        segments = pathmod.split(root_path)[1]
        if segments:
            names.append(segments[-1])

    return Secrets(
        paths=tuple(s for s in dict.fromkeys(full) if s),
        identity=tuple(s for s in dict.fromkeys(identity) if s),
        names=tuple(s for s in dict.fromkeys(names) if s),
    )
