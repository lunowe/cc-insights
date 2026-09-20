"""Flavor-aware path reasoning.

Two things are being protected here.

The first is that nothing changed for POSIX. `project_id = hash(root_path)` and
`root_path = paths.normalize(cwd)`, so if `normalize` ever disagrees with the
`os.path.normpath` it replaced, every existing project forks into a second row
and the whole history doubles. The `_legacy_*` helpers below are verbatim
copies of the pre-v2 implementations, duplicated on purpose: they are the
oracle, and a regression has to fail a test rather than a dashboard.

The second is that a Windows path is reasoned about correctly *on this Mac*.
That is not a Windows test running on the wrong OS -- it is the actual v2 case.
Once machines sync, the box rendering the dashboard is usually not the box the
path came from.
"""

from __future__ import annotations

import ntpath
import os
import posixpath

import pytest

from cc_insights import grouping, ids, ingest, paths

POSIX_CORPUS = [
    "/",
    "/Users/me",
    "/Users/me/Coding/CC-Insights",
    "/Users/me/Coding/CC-Insights/",
    "/Users/me/Coding/./CC-Insights",
    "/Users/me/Coding/x/../CC-Insights",
    "/Users/me/Coding/atlas-chat/.claude/worktrees/tenant-2g",
    "/Users/me/.t3/worktrees/atlas-chat/t3code-0c9cc823",
    "/home/ci/work",
    "/srv/git/repo.git",
    "relative/path",
    "/a/b//c",
]

WINDOWS_CORPUS = [
    r"C:\Users\you\Coding\CC-Insights",
    r"C:/Users/you/Coding/CC-Insights",
    r"C:\Users\you\Coding\atlas-chat\.claude\worktrees\tenant-2g",
    r"D:\work\repo",
    r"\\build01\share\repo",
]


# ------------------------------------------------------- the POSIX oracle --


def _legacy_project_root(cwd: str) -> str:
    """ingest.project_root, as it was before `paths` existed."""
    return os.path.normpath(cwd.strip())


def _legacy_project_name(root_path: str) -> str:
    return os.path.basename(root_path.rstrip("/")) or root_path


def _legacy_norm_dir(path: str) -> str:
    """grouping._norm_dir, as it was before `paths` existed."""
    return os.path.normpath(path).rstrip("/") or "/"


def _legacy_is_ancestor(parent: str, child: str) -> bool:
    """The `startswith(... + "/")` test grouping._ancestors used to run."""
    prefix = child.rstrip("/")
    return prefix != parent and prefix.startswith(parent.rstrip("/") + "/")


@pytest.mark.parametrize("path", POSIX_CORPUS)
def test_normalize_matches_the_normpath_it_replaced(path):
    """The id contract: a POSIX root_path must hash to what it always did."""
    assert paths.normalize(path) == _legacy_project_root(path)


@pytest.mark.parametrize("path", POSIX_CORPUS)
def test_project_root_is_unchanged_for_posix(path):
    assert ingest.project_root(path) == _legacy_project_root(path)


@pytest.mark.parametrize("path", POSIX_CORPUS)
def test_project_name_is_unchanged_for_posix(path):
    assert ingest.project_name(path) == _legacy_project_name(path)


@pytest.mark.parametrize("path", POSIX_CORPUS)
def test_key_matches_the_norm_dir_it_replaced(path):
    """Group ids for rule 2 are hashed from this. It may not drift."""
    assert paths.key(path) == _legacy_norm_dir(path)


@pytest.mark.parametrize("parent", POSIX_CORPUS)
@pytest.mark.parametrize("child", POSIX_CORPUS)
def test_is_ancestor_matches_the_prefix_test_it_replaced(parent, child):
    assert paths.is_ancestor(parent, child) == _legacy_is_ancestor(parent, child)


def test_is_ancestor_is_stricter_than_a_string_prefix():
    """The one place the new test is deliberately better than the old one."""
    assert not paths.is_ancestor("/a/bc", "/a/bcd")
    assert paths.is_ancestor("/a/bc", "/a/bc/d")


# --------------------------------------------------------------- flavor --


@pytest.mark.parametrize("path", POSIX_CORPUS)
def test_posix_paths_are_posix(path):
    assert paths.flavor(path) == paths.POSIX


@pytest.mark.parametrize("path", WINDOWS_CORPUS)
def test_windows_paths_are_windows(path):
    assert paths.flavor(path) == paths.WINDOWS


def test_an_absolute_posix_path_wins_over_a_backslash_in_a_filename():
    """A Linux file may legally contain a backslash; it is still POSIX."""
    assert paths.flavor("/home/me/weird\\name") == paths.POSIX
    assert paths.basename("/home/me/weird\\name") == "weird\\name"


def test_flavor_does_not_depend_on_the_host_os():
    """The whole point: this Mac must answer for a Windows path."""
    assert paths.flavor(r"C:\Users\you\repo") == paths.WINDOWS
    assert paths.LOCAL == paths.POSIX or os.name == "nt"


# --------------------------------------------------------- windows shapes --


@pytest.mark.parametrize(
    ("path", "anchor", "segs"),
    [
        (r"C:\Users\you\repo", "C:\\", ["Users", "you", "repo"]),
        (r"C:/Users/you/repo", "C:\\", ["Users", "you", "repo"]),
        (r"\\build01\share\repo", "\\\\build01\\share\\", ["repo"]),
        ("/Users/me/repo", "/", ["Users", "me", "repo"]),
        ("relative/path", "", ["relative", "path"]),
    ],
)
def test_split_separates_the_anchor_from_the_segments(path, anchor, segs):
    assert paths.split(path) == (anchor, segs)


def test_basename_of_a_windows_path_read_on_a_mac():
    """os.path.basename returns the whole string here. That is the bug."""
    win = r"C:\Users\you\Coding\CC-Insights"
    assert os.path.basename(win) == win, "precondition: os.path is wrong here"
    assert paths.basename(win) == "CC-Insights"
    assert ingest.project_name(win) == "CC-Insights"


def test_windows_comparison_folds_case_and_separators():
    assert paths.same(r"C:\Users\You\Repo", "c:/users/you/repo")
    assert paths.is_ancestor(r"C:\Users\You", r"c:\users\you\repo")


def test_posix_comparison_does_not_fold_case():
    assert not paths.same("/Users/me/Repo", "/Users/me/repo")
    assert not paths.is_ancestor("/Users/Me", "/Users/me/repo")


def test_paths_from_different_machines_never_relate():
    assert not paths.is_ancestor("/Users/me", r"C:\Users\me\repo")
    assert not paths.is_ancestor(r"C:\Users\me", "/Users/me/repo")


@pytest.mark.parametrize("path", ["/", "C:\\", r"\\build01\share"])
def test_roots_are_roots(path):
    assert paths.is_root(path)


@pytest.mark.parametrize(
    "path", ["/Users", "/Users/me", "/home/me", "/root", r"C:\Users", r"C:\Users\you"]
)
def test_home_like_layouts_are_recognized_without_asking_the_os(path):
    assert paths.is_home_like(path)


@pytest.mark.parametrize(
    "path", ["/Users/me/Coding", "/home/me/work", r"C:\Users\you\Coding", "/usr/local", "/srv"]
)
def test_a_real_project_root_is_not_home_like(path):
    assert not paths.is_home_like(path)


def test_abbreviate_home_uses_the_paths_own_separator():
    assert paths.abbreviate_home("/Users/me/Coding/x", "/Users/me") == "~/Coding/x"
    assert paths.abbreviate_home(r"C:\Users\you\Coding\x", r"C:\Users\you") == r"~\Coding\x"
    assert paths.abbreviate_home("/Users/me", "/Users/me") == "~"
    assert paths.abbreviate_home(r"C:\Users\you\x", "/Users/me") == r"C:\Users\you\x"


def test_normalize_uses_the_right_module_per_flavor():
    assert paths.normalize(r"C:/Users/you/../me") == ntpath.normpath(r"C:/Users/you/../me")
    assert paths.normalize("/Users/me/../you") == posixpath.normpath("/Users/me/../you")


# ----------------------------------------------------- grouping, on Windows --


def test_worktree_shape_reads_a_windows_layout():
    shape = grouping.worktree_shape(r"C:\Users\you\Coding\atlas-chat\.claude\worktrees\tenant-2g")
    assert shape is not None
    assert shape.repo == "atlas-chat"
    assert shape.ancestor == r"C:\Users\you\Coding\atlas-chat"
    assert shape.shape == ".claude/worktrees"


def test_worktree_shape_matches_layout_names_case_insensitively_on_windows():
    shape = grouping.worktree_shape(r"C:\Users\you\Coding\Repo\.Claude\Worktrees\wt")
    assert shape is not None and shape.repo == "Repo"


def test_worktree_shape_is_case_sensitive_on_posix():
    """A POSIX filesystem distinguishes `.Claude` from `.claude`, so we must."""
    assert grouping.worktree_shape("/Users/me/Coding/Repo/.Claude/worktrees/wt") is None
    assert grouping.worktree_shape("/Users/me/Coding/Repo/.claude/worktrees/wt") is not None


def test_worktree_shape_reads_the_t3_windows_layout():
    shape = grouping.worktree_shape(r"C:\Users\you\.t3\worktrees\atlas-chat\t3code-0c9c")
    assert shape is not None
    assert shape.repo == "atlas-chat"
    assert shape.ancestor is None


def test_a_windows_home_directory_cannot_anchor_grouping():
    """The failure this prevents: every repo on a colleague's box in one group."""
    assert not grouping.anchorable(r"C:\Users\you")
    assert not grouping.anchorable(r"C:\Users")
    assert not grouping.anchorable("C:\\")
    assert grouping.anchorable(r"C:\Users\you\Coding")


def test_a_posix_home_on_a_foreign_host_cannot_anchor_either():
    assert not grouping.anchorable("/home/colleague")
    assert grouping.anchorable("/home/colleague/work")


# --------------------------------------- the whole ladder, on windows paths --
#
# These run on whatever OS the suite runs on, which is the point: after v2 the
# machine rendering the dashboard is usually not the machine the path came
# from. Every assertion below fails if grouping asks `os.path` instead of
# `paths`, because on a Mac `os.path.basename` returns the whole Windows
# string and every project on a colleague's box collapses into one group.


HOST = "h1"


def _add(conn, root_path, *, remote=None, exists=None):
    pid = ids.project_id(root_path)
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, 'box', 'test', 0, 0)",
        (HOST,),
    )
    conn.execute(
        "INSERT INTO project (project_id, root_path, name) VALUES (?, ?, ?)",
        (pid, root_path, ingest.project_name(root_path)),
    )
    if remote is not None or exists is not None:
        conn.execute(
            """INSERT INTO project_probe
               (project_id, host_id, git_remote, path_exists, detected_at)
               VALUES (?, ?, ?, ?, 0)""",
            (pid, HOST, remote, exists),
        )
    return pid


def _groups(conn):
    """root_path -> group name, for every project that got one."""
    return {
        r["root_path"]: r["name"]
        for r in conn.execute(
            """SELECT p.root_path, g.name FROM project p
               JOIN project_group g ON g.group_id = p.group_id"""
        )
    }


def test_a_windows_worktree_adopts_its_parent_checkout(conn):
    _add(conn, r"C:\Users\you\Coding\atlas-chat",
         remote="https://github.com/vfl/atlas-chat.git", exists=1)
    _add(conn, r"C:\Users\you\Coding\atlas-chat\.claude\worktrees\driftwood", exists=0)

    grouping.detect(conn, probe_fs=False)

    assert set(_groups(conn).values()) == {"atlas-chat"}
    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 1


def test_a_windows_t3_worktree_finds_the_checkout_it_belongs_to(conn):
    """The `.t3` layout names the repo but not where it lives.

    Rule 3 has to find the main checkout by basename. `os.path.basename` on a
    Mac hands back `C:\\Users\\you\\Coding\\atlas-chat` whole, which matches
    nothing, and the worktree silently becomes a group of its own.
    """
    _add(conn, r"C:\Users\you\Coding\atlas-chat",
         remote="https://github.com/vfl/atlas-chat.git", exists=1)
    _add(conn, r"C:\Users\you\.t3\worktrees\atlas-chat\t3code-0c9c", exists=0)

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 1
    assert set(_groups(conn).values()) == {"atlas-chat"}


def test_a_windows_ancestor_chain_is_one_group(conn):
    _add(conn, r"C:\work\outer")
    _add(conn, r"C:\work\outer\inner")
    _add(conn, r"C:\work\outer\inner\deep")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 1
    assert set(_groups(conn).values()) == {"outer"}


def test_a_windows_ancestor_is_whole_segments_only(conn):
    _add(conn, r"C:\work\repo")
    _add(conn, r"C:\work\repo-loam")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0


def test_two_machines_two_separators_one_repo(conn):
    """The v2 payoff: one repo checked out on a Mac and a PC is one group.

    Nothing clever makes this work -- the remote is the join key and it was
    already normalized. It is asserted because it is the whole reason the
    schema carries host_id on every row.
    """
    _add(conn, "/Users/me/Coding/CC-Insights", remote="git@github.com:lunowe/cc-insights.git")
    _add(conn, r"C:\Users\you\Coding\CC-Insights",
         remote="https://github.com/lunowe/cc-insights.git")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 1
    assert len(_groups(conn)) == 2


def test_a_windows_home_directory_does_not_swallow_the_repos_under_it(conn):
    """`anchorable`'s rule, for a home directory this machine cannot look up."""
    _add(conn, r"C:\Users\you")
    _add(conn, r"C:\Users\you\slm-finetune")
    _add(conn, r"C:\Users\you\classification")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0


def test_probing_leaves_a_path_from_another_machine_alone(conn):
    """`os.path.isdir` would call a colleague's live worktree dead."""
    _add(conn, r"C:\Users\you\Coding\repo", remote="https://github.com/o/repo.git", exists=1)
    _add(conn, "/nonexistent/local/path", exists=1)

    grouping.detect(conn, probe_fs=True)

    rows = {
        r["root_path"]: r["path_exists"]
        for r in conn.execute(
            """SELECT p.root_path, pp.path_exists
               FROM project p JOIN project_probe pp ON pp.project_id = p.project_id"""
        )
    }
    assert rows[r"C:\Users\you\Coding\repo"] == 1, "the cached answer must survive"
    assert rows["/nonexistent/local/path"] == 0, "a local path is still probed"
