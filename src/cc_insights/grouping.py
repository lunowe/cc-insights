"""Project-group detection: one logical project, many on-disk paths.

`project_id = hash(root_path)` is immutable identity and is never rewritten
here. Grouping is a separate, mutable layer (`project_group`,
`project.group_id`) that can be re-run, corrected or thrown away. Nothing in
this module touches a span, a session or a project_id.

The ladder (docs/GROUPING.md). Each project takes the FIRST rule that matches:

    1 git_remote      normalized origin URL shared by several checkouts
    2 git_common_dir  a worktree resolved to its main repo (no remote)
    3 path_worktree   a known worktree path shape -- works when the path is GONE
    4 path_ancestor   the path sits inside another project's root
    5 (none)          ungrouped; renders as a group of one, which is legal

Rules 3 and 4 *adopt* the ancestor's rule-1 group when one exists, so a
worktree and its parent checkout land in the same group rather than in two
same-named ones. That is why the passes run strictly in ladder order and why
rule 4 resolves its anchor transitively: ``slm-finetune/latex`` follows
``slm-finetune`` wherever `slm-finetune` itself ended up.

An ancestor must be a real parent, never a home directory -- see `anchorable`.

Filesystem probing is cached into `project.git_remote` / `git_common_dir` /
`path_exists` / `detected_at`, so a path that later disappears keeps what was
learned while it existed, and so `detect(probe_fs=False)` is a pure function of
the database -- which is what makes the tests hermetic and offline.

Credentials are stripped from every remote before it is stored. Real remotes
here carry a username; this database is designed to be shareable.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cc_insights.db import now_ms
from cc_insights.ids import make_id

# A git call on a stale network mount can hang forever. Five seconds is plenty
# for a local `git remote get-url`, and one bad path must never abort a run.
GIT_TIMEOUT_S = 5.0

ORIGIN_GIT_REMOTE = "git_remote"
ORIGIN_GIT_COMMON_DIR = "git_common_dir"
ORIGIN_PATH_WORKTREE = "path_worktree"
ORIGIN_PATH_ANCESTOR = "path_ancestor"
ORIGIN_MANUAL = "manual"

#: Ladder order, strongest evidence first. `detect` runs one pass per entry.
RULES = (ORIGIN_GIT_REMOTE, ORIGIN_GIT_COMMON_DIR, ORIGIN_PATH_WORKTREE, ORIGIN_PATH_ANCESTOR)

_H = 3_600_000.0  # ms per hour


# --------------------------------------------------------------------------
# remote URL normalization
# --------------------------------------------------------------------------

# scp-like syntax: [user@]host:path, where path does NOT start with "/".
# A real URL ("https://host/...") is excluded because its path starts with "/".
_SCP_RE = re.compile(r"^(?:(?P<user>[^/@]+)@)?(?P<host>[^/:@]+):(?P<path>[^/].*)$")

# ssh/git are the same repository as https; normalizing them together is what
# lets an ssh checkout and an https checkout of one repo share a group.
_SSH_LIKE = {"ssh", "git", "git+ssh", "http", "https"}


def _strip_git_suffix(path: str) -> str:
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path.rstrip("/")


def normalize_remote(url: str | None) -> str | None:
    """Canonical, credential-free form of a git remote URL.

    ``git@github.com:owner/repo.git`` -> ``https://github.com/owner/repo``.
    Userinfo (``user@`` and any ``:password``) is dropped, a trailing ``.git``
    and trailing ``/`` are stripped, and only the host is lowercased -- a path
    may be case-sensitive.
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text:
        return None

    if "://" in text:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        try:
            host = (parts.hostname or "").lower()
            port = parts.port
        except ValueError:  # malformed port
            host, port = "", None
        path = _strip_git_suffix(parts.path)
        if not host:
            # file:/// and friends: no authority to clean, keep the shape.
            return urlunsplit((scheme, "", path, "", "")) or None
        if scheme in _SSH_LIKE:
            scheme = "https"
        netloc = f"{host}:{port}" if port else host
        if path and not path.startswith("/"):
            path = "/" + path
        return urlunsplit((scheme, netloc, path, "", ""))

    m = _SCP_RE.match(text)
    if m:
        host = m.group("host").lower()
        path = _strip_git_suffix("/" + m.group("path").lstrip("/"))
        return f"https://{host}{path}"

    # A bare local path (`/srv/git/repo.git`). Still a legitimate origin.
    return _strip_git_suffix(text) or None


@dataclass(slots=True, frozen=True)
class Forge:
    """What a normalized remote says about where the code lives."""

    forge: str | None = None
    owner: str | None = None
    repo: str | None = None
    web_url: str | None = None


def parse_forge(normalized_url: str | None) -> Forge:
    """Forge metadata from a normalized remote.

    GitHub is recognized properly (owner/repo and a browsable URL). Everything
    else is stored generically -- the host as `forge` and the last path segment
    as `repo` -- rather than guessing a URL shape we have not verified. Azure
    DevOps, for instance, spells a repo ``<org>/<project>/_git/<repo>``, so
    there is no honest `owner` to record.
    """
    if not normalized_url:
        return Forge()

    if "://" in normalized_url:
        parts = urlsplit(normalized_url)
        try:
            host = (parts.hostname or "").lower()
        except ValueError:
            host = ""
        path = parts.path
    else:
        host, path = "", normalized_url

    segs = [s for s in path.split("/") if s]
    repo = segs[-1] if segs else None

    if host == "github.com" or host.endswith(".github.com"):
        owner = segs[-2] if len(segs) >= 2 else None
        web = f"https://github.com/{owner}/{repo}" if owner and repo else normalized_url
        return Forge("github", owner, repo, web)

    return Forge(host or None, None, repo, normalized_url if host else None)


# --------------------------------------------------------------------------
# worktree path shapes -- these must work when the path no longer exists
# --------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class WorktreeShape:
    """A path recognized as a worktree of `repo`.

    `ancestor` is the parent checkout's root path when the shape spells it out
    (the `.claude/worktrees` layout does; `.t3` and `conductor` name the repo
    but keep the worktrees outside it, so only `repo` is known).
    """

    repo: str
    ancestor: str | None = None
    shape: str = ""


def _segments(path: str) -> list[str]:
    return [s for s in path.replace("\\", "/").split("/") if s]


def worktree_shape(root_path: str) -> WorktreeShape | None:
    """Recognize the three known worktree layouts. Never touches the disk."""
    segs = _segments(root_path)
    absolute = root_path.startswith("/")

    # `.../<repo>/.claude/worktrees/<name>` -- scan from the right so a nested
    # checkout resolves to the innermost repository.
    for i in range(len(segs) - 3, 0, -1):
        if segs[i] == ".claude" and segs[i + 1] == "worktrees":
            prefix = "/".join(segs[:i])
            ancestor = ("/" + prefix) if absolute else prefix
            return WorktreeShape(segs[i - 1], ancestor, ".claude/worktrees")

    # `~/.t3/worktrees/<repo>/<name>` and `~/conductor/workspaces/<repo>/<name>`
    for parent, child in ((".t3", "worktrees"), ("conductor", "workspaces")):
        for i in range(len(segs) - 4, -1, -1):
            if segs[i] == parent and segs[i + 1] == child:
                return WorktreeShape(segs[i + 2], None, f"{parent}/{child}")
    return None


# --------------------------------------------------------------------------
# filesystem probe
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Probe:
    exists: bool = False
    git_ran: bool = False           # the binary executed; its answer is usable
    remote: str | None = None       # raw, not yet normalized
    common_dir: str | None = None


def _git(path: str, args: list[str]) -> tuple[bool, str | None]:
    """Run `git -C path ...`. Returns (the binary ran, its stdout or None).

    A missing git, a timeout, or any OS-level failure returns (False, None) so
    the caller can tell "git said no" from "we could not ask", and keep the
    cached answer in the second case.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if cp.returncode != 0:
        return True, None
    out = cp.stdout.strip()
    return True, out or None


def probe_path(root_path: str) -> Probe:
    """Ask the filesystem what it knows about one project root."""
    p = Probe(exists=os.path.isdir(root_path))
    if not p.exists:
        return p

    ran, remote = _git(root_path, ["remote", "get-url", "origin"])
    p.git_ran = ran
    p.remote = remote

    ran2, common = _git(root_path, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if not ran2 or common is None:
        # --path-format needs git >= 2.31; fall back and absolutize ourselves.
        ran2, common = _git(root_path, ["rev-parse", "--git-common-dir"])
        if common and not os.path.isabs(common):
            common = os.path.normpath(os.path.join(root_path, common))
    p.git_ran = p.git_ran or ran2
    p.common_dir = common
    return p


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Project:
    project_id: str
    root_path: str
    name: str
    group_id: str | None = None
    pinned: bool = False
    git_remote: str | None = None
    git_common_dir: str | None = None
    path_exists: int | None = None
    detected_at: int | None = None


@dataclass(slots=True)
class GroupPlan:
    group_id: str
    name: str
    origin: str
    match_key: str | None
    remote_url: str | None = None
    forge: str | None = None
    owner: str | None = None
    repo: str | None = None
    web_url: str | None = None
    members: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GroupingResult:
    """What one `detect()` did, or -- with `dry_run` -- what it would do."""

    dry_run: bool = False
    plans: list[GroupPlan] = field(default_factory=list)
    assignments: dict[str, str | None] = field(default_factory=dict)
    rule_of: dict[str, str] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    groups_created: int = 0
    groups_deleted: int = 0
    projects_grouped: int = 0
    projects_moved: int = 0
    pinned_skipped: int = 0
    ungrouped: int = 0
    probed: int = 0
    probe_failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.groups_created or self.groups_deleted or self.projects_moved)


def group_id_for(origin: str, match_key: str | None) -> str:
    """Stable id from the natural key, exactly like every other id here."""
    return make_id(origin, match_key)


def load_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute(
        """SELECT project_id, root_path, name, group_id, group_pinned,
                  git_remote, git_common_dir, path_exists, detected_at
           FROM project ORDER BY root_path"""
    ).fetchall()
    return [
        Project(
            project_id=r["project_id"],
            root_path=r["root_path"],
            name=r["name"],
            group_id=r["group_id"],
            pinned=bool(r["group_pinned"]),
            git_remote=r["git_remote"],
            git_common_dir=r["git_common_dir"],
            path_exists=r["path_exists"],
            detected_at=r["detected_at"],
        )
        for r in rows
    ]


class _Planner:
    """Runs the ladder over an in-memory snapshot of `project`.

    Kept separate from the writing so `--dry-run` and `probe_fs=False` exercise
    exactly the same code path as a real run.
    """

    def __init__(self, projects: list[Project]) -> None:
        self.projects = projects
        self.by_path = {p.root_path: p for p in projects}
        self.plans: dict[str, GroupPlan] = {}
        self.assigned: dict[str, str] = {}   # project_id -> group_id
        self.rule_of: dict[str, str] = {}    # project_id -> origin that matched
        # Pinned projects keep whatever a human gave them; they are never
        # reassigned, but they may still anchor their descendants.
        for p in projects:
            if p.pinned and p.group_id:
                self.assigned[p.project_id] = p.group_id
                self.rule_of[p.project_id] = ORIGIN_MANUAL

        self.by_basename: dict[str, list[Project]] = {}
        for p in projects:
            self.by_basename.setdefault(os.path.basename(p.root_path).lower(), []).append(p)

    # ---------------------------------------------------------------- utils

    @property
    def movable(self) -> list[Project]:
        return [p for p in self.projects if not p.pinned]

    def unassigned(self) -> list[Project]:
        return [p for p in self.movable if p.project_id not in self.assigned]

    def group_of(self, p: Project) -> str | None:
        return self.assigned.get(p.project_id)

    def ensure_plan(
        self,
        origin: str,
        match_key: str,
        name: str,
        *,
        forge: Forge | None = None,
        remote_url: str | None = None,
    ) -> str:
        gid = group_id_for(origin, match_key)
        plan = self.plans.get(gid)
        if plan is None:
            f = forge or Forge()
            plan = GroupPlan(
                group_id=gid,
                name=name or match_key,
                origin=origin,
                match_key=match_key,
                remote_url=remote_url,
                forge=f.forge,
                owner=f.owner,
                repo=f.repo,
                web_url=f.web_url,
            )
            self.plans[gid] = plan
        return gid

    def assign(self, p: Project, gid: str, rule: str) -> None:
        if p.pinned or p.project_id in self.assigned:
            return
        self.assigned[p.project_id] = gid
        self.rule_of[p.project_id] = rule
        plan = self.plans.get(gid)
        if plan is not None and p.project_id not in plan.members:
            plan.members.append(p.project_id)

    # ------------------------------------------------------------ the rules

    def rule_git_remote(self) -> None:
        for p in self.unassigned():
            norm = normalize_remote(p.git_remote)
            if not norm:
                continue
            forge = parse_forge(norm)
            gid = self.ensure_plan(
                ORIGIN_GIT_REMOTE,
                norm,
                forge.repo or norm,
                forge=forge,
                remote_url=norm,
            )
            self.assign(p, gid, ORIGIN_GIT_REMOTE)

    def rule_git_common_dir(self) -> None:
        # Index the rule-1 groups by the checkout each one was found in, so a
        # remote-less worktree can fold into its main repo's group.
        remote_group_by_path: dict[str, str] = {}
        remote_group_by_common: dict[str, str] = {}
        for p in self.projects:
            gid = self.group_of(p)
            if not gid or self.rule_of.get(p.project_id) != ORIGIN_GIT_REMOTE:
                continue
            remote_group_by_path[p.root_path] = gid
            if p.git_common_dir:
                remote_group_by_common.setdefault(_norm_dir(p.git_common_dir), gid)

        for p in self.unassigned():
            if not p.git_common_dir:
                continue
            common = _norm_dir(p.git_common_dir)
            main_root = os.path.dirname(common) if os.path.basename(common) == ".git" else common
            adopted = remote_group_by_path.get(main_root) or remote_group_by_common.get(common)
            if adopted:
                self.assign(p, adopted, ORIGIN_GIT_COMMON_DIR)
                continue
            name = os.path.basename(main_root) or common
            gid = self.ensure_plan(ORIGIN_GIT_COMMON_DIR, common, name)
            self.assign(p, gid, ORIGIN_GIT_COMMON_DIR)

    def _repo_anchor(self, repo: str, exclude: str) -> Project | None:
        """A known project that looks like the main checkout of `repo`.

        Preferred: one that already has a rule-1 (git_remote) group -- that is
        the adoption the design calls for. Ties break on the shortest path so
        the answer does not depend on row order.
        """
        candidates = [
            c
            for c in self.by_basename.get(repo.lower(), [])
            if c.project_id != exclude
            and worktree_shape(c.root_path) is None
            and anchorable(c.root_path)
        ]
        if not candidates:
            return None
        with_remote = [
            c for c in candidates if self.rule_of.get(c.project_id) == ORIGIN_GIT_REMOTE
        ]
        pool = with_remote or [c for c in candidates if self.group_of(c)] or candidates
        return min(pool, key=lambda c: (len(c.root_path), c.root_path))

    def rule_path_worktree(self) -> None:
        for p in self.unassigned():
            shape = worktree_shape(p.root_path)
            if shape is None:
                continue

            # 1. The shape spelled out the parent checkout -- follow it.
            adopted = None
            if shape.ancestor and anchorable(shape.ancestor):
                parent = self.by_path.get(shape.ancestor)
                if parent is not None:
                    adopted = self.group_of(parent)
                    if adopted is None and not parent.pinned:
                        gid = self.ensure_plan(
                            ORIGIN_PATH_WORKTREE, shape.repo, shape.repo
                        )
                        self.assign(parent, gid, ORIGIN_PATH_WORKTREE)
                        adopted = gid

            # 2. Otherwise the shape only named the repo; find that checkout.
            if adopted is None:
                anchor = self._repo_anchor(shape.repo, p.project_id)
                if anchor is not None:
                    adopted = self.group_of(anchor)

            if adopted is None:
                adopted = self.ensure_plan(ORIGIN_PATH_WORKTREE, shape.repo, shape.repo)
            self.assign(p, adopted, ORIGIN_PATH_WORKTREE)

    def _ancestors(self, p: Project) -> list[Project]:
        """Every project root that legitimately contains this path, longest first.

        `$HOME` and everything at or above it are filtered out here rather
        than at the call site, so no rule can accidentally anchor on them.
        """
        prefix = p.root_path.rstrip("/")
        found = [
            q
            for q in self.projects
            if q.project_id != p.project_id
            and q.root_path != prefix
            and prefix.startswith(q.root_path.rstrip("/") + "/")
            and anchorable(q.root_path)
        ]
        found.sort(key=lambda q: len(q.root_path.rstrip("/")), reverse=True)
        return found

    def rule_path_ancestor(self) -> None:
        # Shallowest first, so an anchor is resolved before its descendants and
        # a chain (slm-finetune/latex -> slm-finetune -> ...) lands in one
        # group rather than two.
        for p in sorted(self.unassigned(), key=lambda q: len(_segments(q.root_path))):
            if p.project_id in self.assigned:
                continue
            for anc in self._ancestors(p):
                gid = self.group_of(anc)
                if gid:
                    self.assign(p, gid, ORIGIN_PATH_ANCESTOR)
                    break
                if anc.pinned:
                    # A human parked this one somewhere with no group; we may
                    # not move it, so it cannot anchor. Try the next ancestor.
                    continue
                gid = self.ensure_plan(ORIGIN_PATH_ANCESTOR, anc.root_path, anc.name)
                self.assign(anc, gid, ORIGIN_PATH_ANCESTOR)
                self.assign(p, gid, ORIGIN_PATH_ANCESTOR)
                break

    def run(self) -> None:
        self.rule_git_remote()
        self.rule_git_common_dir()
        self.rule_path_worktree()
        self.rule_path_ancestor()


def _norm_dir(path: str) -> str:
    return os.path.normpath(path).rstrip("/") or "/"


def anchorable(root_path: str) -> bool:
    """May this project root adopt the paths that sit inside it?

    A home directory is not a project -- it is the absence of one. `~` is a
    project row on this corpus only because someone once ran an agent there
    for 0.0 h, and letting that accident anchor rule 4 files three unrelated
    repositories (`slm-finetune`, `atlas-chat-loam`, `classification`)
    under one meaningless group that a human then has to unpick by hand. An
    honest group of one costs a row; a false grouping costs trust in every
    number on the page.

    So $HOME, a filesystem root, and anything at or above $HOME are refused.
    A real parent *below* home still anchors normally --
    `slm-finetune/latex` follows `slm-finetune`. What finds no legitimate
    ancestor stays ungrouped, which the design already calls legal and renders
    as a group of one.

    `Path.home()` is resolved per call rather than baked in at import, so this
    stays correct on another machine and under a test's patched HOME.
    """
    path = _norm_dir(root_path)
    if path == os.path.dirname(path):        # "/" and any drive root
        return False
    home = _norm_dir(str(Path.home()))
    return not (path == home or home.startswith(path + "/"))


# --------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------


def detect(
    conn: sqlite3.Connection,
    *,
    probe_fs: bool = True,
    dry_run: bool = False,
) -> GroupingResult:
    """Run the detection ladder and (unless `dry_run`) write the result.

    With `probe_fs=False` nothing is executed and nothing is stat-ed: the run
    is a pure function of the cached `git_remote` / `git_common_dir` columns
    and the logged paths. That is the hermetic, offline mode the tests use.

    Re-running changes nothing the second time: ids are derived from the
    natural key, names of existing groups are preserved, and rows are only
    written when a value actually differs.
    """
    result = GroupingResult(dry_run=dry_run)
    projects = load_projects(conn)

    if probe_fs:
        _probe_all(conn, projects, result, write=not dry_run)

    planner = _Planner(projects)
    planner.run()

    result.plans = list(planner.plans.values())
    result.rule_of = dict(planner.rule_of)
    for p in projects:
        result.assignments[p.project_id] = planner.assigned.get(p.project_id)
    result.pinned_skipped = sum(1 for p in projects if p.pinned)
    result.projects_grouped = sum(1 for gid in result.assignments.values() if gid)
    result.ungrouped = sum(1 for gid in result.assignments.values() if not gid)
    for p in projects:
        if p.pinned:
            continue
        rule = planner.rule_of.get(p.project_id)
        if rule:
            result.by_rule[rule] = result.by_rule.get(rule, 0) + 1

    _score(conn, projects, result)
    if not dry_run:
        _apply(conn, projects, result)
    return result


def _probe_all(
    conn: sqlite3.Connection,
    projects: list[Project],
    result: GroupingResult,
    *,
    write: bool,
) -> None:
    ts = now_ms()
    for p in projects:
        try:
            probe = probe_path(p.root_path)
        except Exception as exc:                       # never abort the run
            result.probe_failures.append(f"{p.root_path}: {exc}")
            continue
        result.probed += 1

        p.path_exists = 1 if probe.exists else 0
        p.detected_at = ts
        if probe.exists and probe.git_ran:
            # Only overwrite when git actually answered. A path that has since
            # disappeared -- or a machine without git -- keeps what it learned.
            p.git_remote = probe.remote
            p.git_common_dir = probe.common_dir

        if write:
            conn.execute(
                """UPDATE project
                   SET git_remote = ?, git_common_dir = ?, path_exists = ?, detected_at = ?
                   WHERE project_id = ?""",
                (p.git_remote, p.git_common_dir, p.path_exists, p.detected_at, p.project_id),
            )


def _score(conn: sqlite3.Connection, projects: list[Project], result: GroupingResult) -> None:
    """Count what would change, whether or not we are about to write it."""
    existing = {
        r["group_id"] for r in conn.execute("SELECT group_id FROM project_group")
    }
    result.groups_created = sum(1 for plan in result.plans if plan.group_id not in existing)
    result.projects_moved = sum(
        1 for p in projects if result.assignments.get(p.project_id) != p.group_id
    )
    planned = {plan.group_id for plan in result.plans}
    kept = {gid for gid in result.assignments.values() if gid}
    doomed = conn.execute(
        "SELECT group_id FROM project_group WHERE origin <> ?", (ORIGIN_MANUAL,)
    ).fetchall()
    result.groups_deleted = sum(
        1 for r in doomed if r["group_id"] not in planned and r["group_id"] not in kept
    )


_GROUP_META = ("remote_url", "forge", "owner", "repo", "web_url")


def _apply(conn: sqlite3.Connection, projects: list[Project], result: GroupingResult) -> None:
    ts = now_ms()

    # 1. Groups first: project.group_id is a foreign key into this table.
    for plan in result.plans:
        row = conn.execute(
            "SELECT * FROM project_group WHERE group_id = ?", (plan.group_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO project_group
                   (group_id, name, origin, match_key, remote_url, forge, owner, repo,
                    web_url, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.group_id, plan.name, plan.origin, plan.match_key, plan.remote_url,
                    plan.forge, plan.owner, plan.repo, plan.web_url, ts, ts,
                ),
            )
            continue
        # A name may have been edited by a human (`cci group rename`); never
        # clobber it. Forge metadata is ours to refresh.
        changes = {c: getattr(plan, c) for c in _GROUP_META if row[c] != getattr(plan, c)}
        if changes:
            sets = ", ".join(f"{c} = ?" for c in changes)
            conn.execute(
                f"UPDATE project_group SET {sets}, updated_at = ? WHERE group_id = ?",
                (*changes.values(), ts, plan.group_id),
            )

    # 2. Project membership. Pinned rows were never assigned, so they cannot
    #    appear here -- but assert it, because silently moving one is the worst
    #    thing this module could do.
    for p in projects:
        target = result.assignments.get(p.project_id)
        if target == p.group_id:
            continue
        if p.pinned:
            raise AssertionError(f"refusing to move pinned project {p.root_path}")
        conn.execute(
            "UPDATE project SET group_id = ? WHERE project_id = ?", (target, p.project_id)
        )

    # 3. Sweep up automatic groups nobody is in any more. Manual groups survive
    #    empty -- a human made them on purpose -- and a group with members
    #    (pinned ones included) is never in this set to begin with.
    conn.execute(
        """DELETE FROM project_group
           WHERE origin <> ?
             AND group_id NOT IN (SELECT group_id FROM project WHERE group_id IS NOT NULL)""",
        (ORIGIN_MANUAL,),
    )


# --------------------------------------------------------------------------
# manual placement
# --------------------------------------------------------------------------


class GroupError(Exception):
    """A user-facing failure: nothing was written."""


class AmbiguousMatch(GroupError):
    def __init__(self, spec: str, candidates: list[str]) -> None:
        super().__init__(f"{spec!r} matches {len(candidates)} projects")
        self.spec = spec
        self.candidates = candidates


def _like(spec: str) -> str:
    escaped = spec.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def find_projects(conn: sqlite3.Connection, spec: str) -> list[sqlite3.Row]:
    """Projects matching a case-insensitive substring of the name or path.

    An exact name or path hit wins outright, so `atlas-chat` picks the
    checkout rather than throwing up its hands over every path containing it.
    """
    exact = conn.execute(
        """SELECT * FROM project
           WHERE lower(name) = ? OR lower(root_path) = ?
           ORDER BY root_path""",
        (spec.lower(), spec.lower()),
    ).fetchall()
    if len(exact) == 1:
        return exact
    return conn.execute(
        r"""SELECT * FROM project
            WHERE lower(name) LIKE ? ESCAPE '\' OR lower(root_path) LIKE ? ESCAPE '\'
            ORDER BY root_path""",
        (_like(spec), _like(spec)),
    ).fetchall()


def resolve_project(conn: sqlite3.Connection, spec: str) -> sqlite3.Row:
    matches = find_projects(conn, spec)
    if not matches:
        raise GroupError(f"no project matches {spec!r}")
    if len(matches) > 1:
        raise AmbiguousMatch(spec, [m["root_path"] for m in matches])
    return matches[0]


def find_groups(conn: sqlite3.Connection, spec: str) -> list[sqlite3.Row]:
    exact = conn.execute(
        "SELECT * FROM project_group WHERE lower(name) = ? ORDER BY name", (spec.lower(),)
    ).fetchall()
    if len(exact) == 1:
        return exact
    return conn.execute(
        r"SELECT * FROM project_group WHERE lower(name) LIKE ? ESCAPE '\' ORDER BY name",
        (_like(spec),),
    ).fetchall()


def resolve_group(conn: sqlite3.Connection, spec: str) -> sqlite3.Row:
    matches = find_groups(conn, spec)
    if not matches:
        raise GroupError(f"no group matches {spec!r}")
    if len(matches) > 1:
        raise AmbiguousMatch(spec, [m["name"] for m in matches])
    return matches[0]


def create_group(conn: sqlite3.Connection, name: str, *, origin: str = ORIGIN_MANUAL) -> str:
    """Create an empty manual group. Returns its group_id."""
    name = name.strip()
    if not name:
        raise GroupError("a group needs a name")
    gid = group_id_for(origin, name)
    if conn.execute("SELECT 1 FROM project_group WHERE group_id = ?", (gid,)).fetchone():
        raise GroupError(f"a group named {name!r} already exists")
    ts = now_ms()
    conn.execute(
        """INSERT INTO project_group (group_id, name, origin, match_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (gid, name, origin, name, ts, ts),
    )
    return gid


def group_for_name(conn: sqlite3.Connection, name: str) -> tuple[str, bool]:
    """Find the group called `name`, creating a manual one if it is new.

    Returns (group_id, created).
    """
    matches = find_groups(conn, name)
    if len(matches) == 1:
        return matches[0]["group_id"], False
    if len(matches) > 1:
        raise AmbiguousMatch(name, [m["name"] for m in matches])
    return create_group(conn, name), True


def pin_projects(conn: sqlite3.Connection, project_ids: list[str], group_id: str) -> int:
    for pid in project_ids:
        conn.execute(
            "UPDATE project SET group_id = ?, group_pinned = 1 WHERE project_id = ?",
            (group_id, pid),
        )
    return len(project_ids)


def unpin_projects(conn: sqlite3.Connection, project_ids: list[str]) -> int:
    """Hand these back to automatic detection: unpinned and unplaced."""
    for pid in project_ids:
        conn.execute(
            "UPDATE project SET group_id = NULL, group_pinned = 0 WHERE project_id = ?", (pid,)
        )
    return len(project_ids)


def rename_group(conn: sqlite3.Connection, group_id: str, new_name: str) -> None:
    new_name = new_name.strip()
    if not new_name:
        raise GroupError("a group needs a name")
    clash = conn.execute(
        "SELECT group_id FROM project_group WHERE lower(name) = ? AND group_id <> ?",
        (new_name.lower(), group_id),
    ).fetchone()
    if clash:
        raise GroupError(f"a group named {new_name!r} already exists")
    conn.execute(
        "UPDATE project_group SET name = ?, updated_at = ? WHERE group_id = ?",
        (new_name, now_ms(), group_id),
    )


# --------------------------------------------------------------------------
# read models for `cci group list`
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Member:
    project_id: str
    root_path: str
    name: str
    hours: float
    pinned: bool
    path_exists: int | None


@dataclass(slots=True)
class GroupView:
    group_id: str | None
    name: str
    origin: str
    remote_url: str | None = None
    forge: str | None = None
    owner: str | None = None
    repo: str | None = None
    web_url: str | None = None
    members: list[Member] = field(default_factory=list)

    @property
    def hours(self) -> float:
        return sum(m.hours for m in self.members)

    @property
    def pinned_count(self) -> int:
        return sum(1 for m in self.members if m.pinned)


def project_hours(conn: sqlite3.Connection) -> dict[str, float]:
    return {
        r[0]: (r[1] or 0) / _H
        for r in conn.execute(
            """SELECT p.project_id, coalesce(sum(sp.ended_at - sp.started_at), 0)
               FROM project p
               LEFT JOIN session s ON s.project_id = p.project_id
               LEFT JOIN span sp ON sp.session_id = s.id
               GROUP BY p.project_id"""
        )
    }


def list_groups(conn: sqlite3.Connection) -> list[GroupView]:
    """Every group with its members, plus one pseudo-group per ungrouped row.

    Sorted by active hours, members likewise: the biggest thing you spent time
    on is the first thing you read.
    """
    hours = project_hours(conn)
    views: dict[str, GroupView] = {}
    for r in conn.execute("SELECT * FROM project_group"):
        views[r["group_id"]] = GroupView(
            group_id=r["group_id"],
            name=r["name"],
            origin=r["origin"],
            remote_url=r["remote_url"],
            forge=r["forge"],
            owner=r["owner"],
            repo=r["repo"],
            web_url=r["web_url"],
        )

    loose: list[GroupView] = []
    for r in conn.execute(
        "SELECT project_id, root_path, name, group_id, group_pinned, path_exists FROM project"
    ):
        m = Member(
            project_id=r["project_id"],
            root_path=r["root_path"],
            name=r["name"],
            hours=hours.get(r["project_id"], 0.0),
            pinned=bool(r["group_pinned"]),
            path_exists=r["path_exists"],
        )
        view = views.get(r["group_id"]) if r["group_id"] else None
        if view is None:
            loose.append(GroupView(None, m.name, "", members=[m]))
        else:
            view.members.append(m)

    out = list(views.values()) + loose
    for v in out:
        v.members.sort(key=lambda m: (-m.hours, m.root_path))
    out.sort(key=lambda v: (-v.hours, v.name.lower()))
    return out
