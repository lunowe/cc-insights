"""Project-group detection.

Everything here runs with ``probe_fs=False`` unless it says otherwise: the
detection ladder is a pure function of the cached `git_remote` /
`git_common_dir` columns and the logged paths, so the whole suite is hermetic
and offline. The worktree tests deliberately use paths that do NOT exist --
on the author's corpus ten logged paths are already gone and hold 18 h, and
recovering them is the reason rule 3 exists at all.

The one test that shells out to git is skipped when git is absent and touches
nothing outside tmp_path.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from cc_insights import cli, grouping, ids

GIT = shutil.which("git")


# ------------------------------------------------------------------ helpers --


def add_project(
    conn: sqlite3.Connection,
    root_path: str,
    *,
    name: str | None = None,
    remote: str | None = None,
    common_dir: str | None = None,
    exists: int | None = None,
    pinned: int = 0,
    group_id: str | None = None,
) -> str:
    pid = ids.project_id(root_path)
    conn.execute(
        """INSERT INTO project
           (project_id, root_path, name, group_id, group_pinned,
            git_remote, git_common_dir, path_exists)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pid,
            root_path,
            name or root_path.rstrip("/").rsplit("/", 1)[-1],
            group_id,
            pinned,
            remote,
            common_dir,
            exists,
        ),
    )
    return pid


def add_time(conn: sqlite3.Connection, project_id: str, hours: float, tag: str = "") -> None:
    """Give a project some active time, so `list_groups` has something to sum."""
    host = "h1"
    conn.execute(
        "INSERT OR IGNORE INTO host (host_id, hostname, os, first_seen, last_seen)"
        " VALUES (?, 'box', 'test', 0, 0)",
        (host,),
    )
    sid = ids.make_id(project_id, tag, "session")
    tid = ids.make_id(sid, "thread")
    ms = int(hours * 3_600_000)
    conn.execute(
        """INSERT INTO session (id, native_id, source, host_id, project_id, started_at, ended_at)
           VALUES (?, ?, 'claude_code', ?, ?, 0, ?)""",
        (sid, sid, host, project_id, ms),
    )
    conn.execute(
        """INSERT INTO thread (id, native_id, session_id, started_at, ended_at)
           VALUES (?, ?, ?, 0, ?)""",
        (tid, tid, sid, ms),
    )
    conn.execute(
        """INSERT INTO span (id, session_id, thread_id, started_at, ended_at, event_count)
           VALUES (?, ?, ?, 0, ?, 2)""",
        (ids.make_id(tid, "span"), sid, tid, ms),
    )


def group_names(conn: sqlite3.Connection) -> dict[str, str]:
    """root_path -> group name, for every project that has one."""
    return {
        r["root_path"]: r["name"]
        for r in conn.execute(
            """SELECT p.root_path, g.name FROM project p
               JOIN project_group g ON g.group_id = p.group_id"""
        )
    }


def dump(conn: sqlite3.Connection) -> tuple:
    return (
        conn.execute(
            "SELECT * FROM project_group ORDER BY group_id"
        ).fetchall(),
        conn.execute(
            """SELECT project_id, group_id, group_pinned, git_remote, git_common_dir,
                      path_exists, detected_at FROM project ORDER BY project_id"""
        ).fetchall(),
    )


# ----------------------------------------------------------- normalize_remote --


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The credential case. A leaked username in a shareable database is a
        # defect, and every real remote on this corpus carries one.
        (
            "https://northwind-ops@dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo",
            "https://dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo",
        ),
        ("https://user:hunter2@github.com/acme/widget.git", "https://github.com/acme/widget"),
        ("git@github.com:owner/repo.git", "https://github.com/owner/repo"),
        ("git@github.com:owner/repo", "https://github.com/owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo/", "https://github.com/owner/repo"),
        # host lowercased, path left alone -- a path may be case-sensitive.
        ("https://GitHub.COM/Owner/Repo.git", "https://github.com/Owner/Repo"),
        ("ssh://git@gitlab.example.com:2222/grp/sub/repo.git",
         "https://gitlab.example.com:2222/grp/sub/repo"),
        ("  https://github.com/owner/repo.git  ", "https://github.com/owner/repo"),
        ("/srv/git/repo.git", "/srv/git/repo"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_remote(raw, expected):
    assert grouping.normalize_remote(raw) == expected


def test_normalize_remote_never_keeps_userinfo():
    for raw in (
        "https://northwind-ops@dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo",
        "https://user:hunter2@github.com/acme/widget.git",
        "git@github.com:owner/repo.git",
        "ssh://deploy@example.org/x/y.git",
    ):
        assert "@" not in (grouping.normalize_remote(raw) or "")


def test_two_spellings_of_one_remote_normalize_equal():
    ssh = grouping.normalize_remote("git@github.com:lunowe/pinecrest.git")
    https = grouping.normalize_remote("https://github.com/lunowe/pinecrest")
    assert ssh == https == "https://github.com/lunowe/pinecrest"


# ------------------------------------------------------------------- forge --


def test_parse_forge_github():
    f = grouping.parse_forge("https://github.com/northwind-labs/atlas-chat")
    assert (f.forge, f.owner, f.repo) == ("github", "northwind-labs", "atlas-chat")
    assert f.web_url == "https://github.com/northwind-labs/atlas-chat"


def test_parse_forge_azure_is_generic_not_guessed():
    """Azure spells a repo <org>/<project>/_git/<repo>; there is no honest owner."""
    url = "https://dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo"
    f = grouping.parse_forge(url)
    assert f.forge == "dev.azure.com"
    assert f.owner is None
    assert f.repo == "platform-monorepo"
    assert f.web_url == url


def test_parse_forge_empty():
    assert grouping.parse_forge(None) == grouping.Forge()


# --------------------------------------------------------- worktree shapes --
# All three shapes, with paths that do not exist. os.path is never consulted.


@pytest.mark.parametrize(
    "path,repo,ancestor",
    [
        (
            "/Users/gone/Coding/atlas-chat/.claude/worktrees/tenant-restricted",
            "atlas-chat",
            "/Users/gone/Coding/atlas-chat",
        ),
        ("/Users/gone/.t3/worktrees/atlas-chat/t3code-7f2738ac", "atlas-chat", None),
        ("/Users/gone/conductor/workspaces/Quickstart/chengdu", "Quickstart", None),
    ],
)
def test_worktree_shape_on_missing_paths(path, repo, ancestor):
    import os

    assert not os.path.exists(path)
    shape = grouping.worktree_shape(path)
    assert shape is not None
    assert shape.repo == repo
    assert shape.ancestor == ancestor


def test_worktree_shape_rejects_plain_paths():
    for path in (
        "/Users/gone/Coding/atlas-chat",
        "/Users/gone/Coding/atlas-chat/backend",
        "/Users/gone/.claude",
        "/Users/gone/.t3/worktrees/OnlyRepo",
    ):
        assert grouping.worktree_shape(path) is None


def test_worktree_shape_picks_the_innermost_checkout():
    shape = grouping.worktree_shape("/a/outer/.claude/worktrees/x/inner/.claude/worktrees/y")
    assert shape.repo == "inner"
    assert shape.ancestor == "/a/outer/.claude/worktrees/x/inner"


# ------------------------------------------------------------- rule 1 & 2 --


def test_rule1_folds_checkouts_sharing_a_remote(conn):
    add_project(conn, "/w/atlas-chat", remote="https://github.com/vfl/atlas-chat.git")
    add_project(conn, "/w/atlas-chat/backend", remote="git@github.com:vfl/atlas-chat.git")
    add_project(conn, "/elsewhere/clone", remote="https://github.com/vfl/atlas-chat")

    r = grouping.detect(conn, probe_fs=False)

    assert len(r.plans) == 1
    assert set(group_names(conn).values()) == {"atlas-chat"}
    g = conn.execute("SELECT * FROM project_group").fetchone()
    assert g["origin"] == "git_remote"
    assert g["remote_url"] == "https://github.com/vfl/atlas-chat"
    assert g["forge"] == "github"
    assert g["owner"] == "vfl"


def test_rule1_stores_the_stripped_remote_only(conn):
    add_project(
        conn,
        "/w/platform-monorepo",
        remote="https://northwind-ops@dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo",
    )
    grouping.detect(conn, probe_fs=False)
    g = conn.execute("SELECT * FROM project_group").fetchone()
    assert "northwind-ops@" not in g["remote_url"]
    assert g["remote_url"] == "https://dev.azure.com/northwind-ops/platform-core/_git/platform-monorepo"
    assert g["forge"] == "dev.azure.com"
    assert g["owner"] is None


def test_rule2_folds_a_remoteless_worktree_into_its_main_repo(conn):
    """The main repo has a remote, so the worktree joins its rule-1 group."""
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git",
                common_dir="/w/repo/.git")
    add_project(conn, "/tmp/wt-a", common_dir="/w/repo/.git")

    grouping.detect(conn, probe_fs=False)

    assert len(conn.execute("SELECT 1 FROM project_group").fetchall()) == 1
    assert set(group_names(conn)) == {"/w/repo", "/tmp/wt-a"}


def test_rule2_makes_its_own_group_when_there_is_no_remote(conn):
    add_project(conn, "/w/local", common_dir="/w/local/.git")
    add_project(conn, "/tmp/wt-b", common_dir="/w/local/.git")

    grouping.detect(conn, probe_fs=False)

    g = conn.execute("SELECT * FROM project_group").fetchone()
    assert g["origin"] == "git_common_dir"
    assert g["name"] == "local"
    assert len(group_names(conn)) == 2


# ----------------------------------------------------------------- rule 3 --


def test_rule3_dead_t3_worktrees_adopt_the_parent_rule1_group(conn):
    """The 9-vs-13-rows case: worktrees whose paths are gone still find home."""
    add_project(conn, "/Users/x/Coding/atlas-chat",
                remote="https://github.com/vfl/atlas-chat.git", exists=1)
    for suffix in ("t3code-7f2738ac", "t3code-0c9cc823", "t3code-d677fddd"):
        add_project(conn, f"/Users/x/.t3/worktrees/atlas-chat/{suffix}", exists=0)

    grouping.detect(conn, probe_fs=False)

    assert len(conn.execute("SELECT 1 FROM project_group").fetchall()) == 1
    assert len(group_names(conn)) == 4
    assert conn.execute("SELECT origin FROM project_group").fetchone()[0] == "git_remote"


def test_rule3_claude_worktree_adopts_its_ancestor_group(conn):
    add_project(conn, "/Users/x/Coding/atlas-chat",
                remote="https://github.com/vfl/atlas-chat.git", exists=1)
    add_project(conn, "/Users/x/Coding/atlas-chat/.claude/worktrees/driftwood", exists=0)

    grouping.detect(conn, probe_fs=False)

    assert len(conn.execute("SELECT 1 FROM project_group").fetchall()) == 1
    assert set(group_names(conn).values()) == {"atlas-chat"}


def test_rule3_conductor_worktrees_group_together_without_a_parent_row(conn):
    add_project(conn, "/Users/x/conductor/workspaces/Quickstart/chengdu", exists=0)
    add_project(conn, "/Users/x/conductor/workspaces/Quickstart/krakow", exists=0)

    grouping.detect(conn, probe_fs=False)

    g = conn.execute("SELECT * FROM project_group").fetchone()
    assert (g["origin"], g["name"]) == ("path_worktree", "Quickstart")
    assert len(group_names(conn)) == 2


def test_rule3_does_not_confuse_a_similarly_named_project(conn):
    """`atlas-chat-loam` must not be swallowed by `atlas-chat`."""
    add_project(conn, "/Users/x/Coding/atlas-chat",
                remote="https://github.com/vfl/atlas-chat.git")
    add_project(conn, "/Users/x/Coding/atlas-chat-loam",
                remote="https://github.com/vfl/atlas-chat-loam.git")
    add_project(conn, "/Users/x/.t3/worktrees/atlas-chat/t3code-1", exists=0)

    grouping.detect(conn, probe_fs=False)

    names = group_names(conn)
    assert names["/Users/x/.t3/worktrees/atlas-chat/t3code-1"] == "atlas-chat"
    assert names["/Users/x/Coding/atlas-chat-loam"] == "atlas-chat-loam"


# ----------------------------------------------------------------- rule 4 --


def test_rule4_longest_ancestor_wins(conn):
    add_project(conn, "/w/outer")
    add_project(conn, "/w/outer/inner")
    add_project(conn, "/w/outer/inner/deep")

    grouping.detect(conn, probe_fs=False)

    # One chain, one group -- not one group per level.
    assert len(conn.execute("SELECT 1 FROM project_group").fetchall()) == 1
    assert set(group_names(conn).values()) == {"outer"}


def test_rule4_adopts_the_ancestors_rule1_group(conn):
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    add_project(conn, "/w/repo/sub")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT origin FROM project_group").fetchone()[0] == "git_remote"
    assert len(group_names(conn)) == 2


def test_rule4_is_a_prefix_of_whole_segments_only(conn):
    add_project(conn, "/w/repo")
    add_project(conn, "/w/repo-loam")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0
    assert group_names(conn) == {}


# ------------------------------------------------- rule 4: the home guard --
#
# A home directory is not a project, it is the absence of one. `~` is a
# project row on the real corpus only because someone once ran an agent there
# for 0.0 h; letting that accident anchor rule 4 filed three unrelated
# repositories under one meaningless group.


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A patched $HOME, so these tests say nothing about the machine running them."""
    home = tmp_path / "Users" / "someone"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert str(Path.home()) == str(home)
    return str(home)


def test_a_home_directory_row_never_becomes_an_anchor(conn, fake_home):
    """The corpus case: `~`, `~/Downloads`, `~/Coding/A`, `~/Coding/B`."""
    add_project(conn, fake_home, name="someone")
    add_project(conn, f"{fake_home}/Downloads")
    add_project(conn, f"{fake_home}/Coding/slm-finetune")
    add_project(conn, f"{fake_home}/Coding/atlas-chat-loam")

    r = grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0
    assert group_names(conn) == {}
    assert r.ungrouped == 4
    assert r.projects_grouped == 0


def test_a_real_parent_below_home_still_anchors(conn, fake_home):
    """`slm-finetune/latex` follows `slm-finetune`; `slm-finetune` stays its own."""
    add_project(conn, fake_home, name="someone")
    add_project(conn, f"{fake_home}/Coding/slm-finetune")
    add_project(conn, f"{fake_home}/Coding/slm-finetune/latex")
    add_project(conn, f"{fake_home}/Downloads")

    grouping.detect(conn, probe_fs=False)

    assert group_names(conn) == {
        f"{fake_home}/Coding/slm-finetune": "slm-finetune",
        f"{fake_home}/Coding/slm-finetune/latex": "slm-finetune",
    }
    g = conn.execute("SELECT * FROM project_group").fetchone()
    assert (g["origin"], g["match_key"]) == ("path_ancestor", f"{fake_home}/Coding/slm-finetune")


def test_nothing_at_or_above_home_may_anchor(conn, fake_home):
    above = str(Path(fake_home).parent)          # .../Users
    add_project(conn, above, name="Users")
    add_project(conn, fake_home, name="someone")
    add_project(conn, f"{fake_home}/Coding/thing")

    grouping.detect(conn, probe_fs=False)

    assert group_names(conn) == {}


@pytest.mark.parametrize("path", ["/", "//", "/."])
def test_a_filesystem_root_may_never_anchor(path):
    assert not grouping.anchorable(path)


def test_anchorable_accepts_an_ordinary_project(fake_home):
    assert grouping.anchorable(f"{fake_home}/Coding/slm-finetune")
    assert grouping.anchorable("/srv/checkouts/repo")
    assert not grouping.anchorable(fake_home)
    assert not grouping.anchorable(fake_home + "/")


def test_the_home_guard_does_not_block_rule_1(conn, fake_home):
    """Evidence beats geography: a shared remote groups even at $HOME."""
    add_project(conn, fake_home, name="someone",
                remote="https://github.com/o/dotfiles.git")
    add_project(conn, f"{fake_home}/Downloads", remote="https://github.com/o/dotfiles.git")

    grouping.detect(conn, probe_fs=False)

    assert set(group_names(conn).values()) == {"dotfiles"}


def test_a_worktree_shape_hanging_off_home_does_not_adopt_it(conn, fake_home):
    add_project(conn, fake_home, name="someone")
    add_project(conn, f"{fake_home}/.claude/worktrees/wt", exists=0)

    grouping.detect(conn, probe_fs=False)

    # It still gets a worktree group of its own; it just does not drag `~` in.
    assert fake_home not in group_names(conn)
    assert conn.execute(
        "SELECT origin FROM project_group"
    ).fetchone()[0] == "path_worktree"


def test_ungrouped_is_legal(conn):
    add_project(conn, "/w/lonely")
    r = grouping.detect(conn, probe_fs=False)
    assert r.ungrouped == 1
    assert r.projects_grouped == 0
    assert conn.execute("SELECT group_id FROM project").fetchone()[0] is None


# ------------------------------------------------------------------ pinned --


def test_auto_never_moves_a_pinned_project(conn):
    """The human's answer is final. This is the escape hatch's whole point."""
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    stray = add_project(conn, "/w/repo/backend", remote="https://github.com/o/repo.git")

    gid = grouping.create_group(conn, "Hand placed")
    grouping.pin_projects(conn, [stray], gid)

    r = grouping.detect(conn, probe_fs=False)

    row = conn.execute(
        "SELECT group_id, group_pinned FROM project WHERE project_id = ?", (stray,)
    ).fetchone()
    assert row["group_id"] == gid
    assert row["group_pinned"] == 1
    assert r.pinned_skipped == 1
    # ...and the manual group survives, because it has a member.
    assert conn.execute(
        "SELECT count(*) FROM project_group WHERE group_id = ?", (gid,)
    ).fetchone()[0] == 1


def test_auto_keeps_an_empty_manual_group(conn):
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    grouping.create_group(conn, "Someday")
    grouping.detect(conn, probe_fs=False)
    assert conn.execute(
        "SELECT count(*) FROM project_group WHERE origin = 'manual'"
    ).fetchone()[0] == 1


def test_auto_sweeps_an_automatic_group_nobody_is_in(conn):
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    grouping.detect(conn, probe_fs=False)
    conn.execute("UPDATE project SET git_remote = 'https://github.com/o/other.git'")

    r = grouping.detect(conn, probe_fs=False)

    assert r.groups_deleted == 1
    assert [r["name"] for r in conn.execute("SELECT name FROM project_group")] == ["other"]


def test_a_pinned_ancestor_anchors_its_children(conn):
    parent = add_project(conn, "/w/tree")
    add_project(conn, "/w/tree/sub")
    gid = grouping.create_group(conn, "Mine")
    grouping.pin_projects(conn, [parent], gid)

    grouping.detect(conn, probe_fs=False)

    assert group_names(conn)["/w/tree/sub"] == "Mine"


# ------------------------------------------------------------ re-runnable --


def test_detect_is_re_runnable_with_no_changes_on_the_second_pass(conn):
    add_project(conn, "/Users/x/Coding/atlas-chat",
                remote="https://github.com/vfl/atlas-chat.git", exists=1)
    add_project(conn, "/Users/x/Coding/atlas-chat/backend",
                remote="git@github.com:vfl/atlas-chat.git", exists=1)
    add_project(conn, "/Users/x/.t3/worktrees/atlas-chat/t3code-1", exists=0)
    add_project(conn, "/Users/x/Coding/plain", common_dir="/Users/x/Coding/plain/.git")
    add_project(conn, "/Users/x/Coding/plain/docs")
    add_project(conn, "/Users/x/lonely")

    first = grouping.detect(conn, probe_fs=False)
    assert first.changed
    before = dump(conn)

    second = grouping.detect(conn, probe_fs=False)

    assert not second.changed
    assert (second.groups_created, second.groups_deleted, second.projects_moved) == (0, 0, 0)
    assert dump(conn) == before
    # ...and a third pass, because idempotence that only holds once is a bug.
    grouping.detect(conn, probe_fs=False)
    assert dump(conn) == before


def test_rename_survives_auto(conn):
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    grouping.detect(conn, probe_fs=False)
    g = grouping.resolve_group(conn, "repo")
    grouping.rename_group(conn, g["group_id"], "The Big One")

    grouping.detect(conn, probe_fs=False)

    assert conn.execute("SELECT name FROM project_group").fetchone()[0] == "The Big One"


# ---------------------------------------------------------------- probing --


def test_probe_of_a_missing_path_keeps_what_was_cached(conn, tmp_path):
    gone = str(tmp_path / "vanished")
    pid = add_project(conn, gone, remote="https://github.com/o/repo.git",
                      common_dir="/w/repo/.git", exists=1)

    grouping.detect(conn, probe_fs=True)

    row = conn.execute("SELECT * FROM project WHERE project_id = ?", (pid,)).fetchone()
    assert row["path_exists"] == 0
    assert row["git_remote"] == "https://github.com/o/repo.git"   # not wiped
    assert row["git_common_dir"] == "/w/repo/.git"
    assert row["detected_at"] is not None
    assert group_names(conn)[gone] == "repo"


def test_probe_of_a_missing_path_runs_no_subprocess(conn, tmp_path, monkeypatch):
    def boom(*a, **kw):  # pragma: no cover - only runs if the guard breaks
        raise AssertionError("git must not be invoked for a path that is gone")

    monkeypatch.setattr(grouping.subprocess, "run", boom)
    add_project(conn, str(tmp_path / "vanished"))
    grouping.detect(conn, probe_fs=True)


def test_one_bad_path_does_not_abort_the_run(conn, tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(root_path):
        calls["n"] += 1
        if "bad" in root_path:
            raise OSError("stale NFS handle")
        return grouping.Probe(exists=False)

    monkeypatch.setattr(grouping, "probe_path", flaky)
    add_project(conn, "/w/bad", remote="https://github.com/o/bad.git")
    add_project(conn, "/w/good", remote="https://github.com/o/good.git")

    r = grouping.detect(conn, probe_fs=True)

    assert calls["n"] == 2
    assert len(r.probe_failures) == 1
    assert group_names(conn)["/w/good"] == "good"


def test_git_probe_uses_a_timeout(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(grouping.subprocess, "run", fake_run)
    ran, out = grouping._git(str(tmp_path), ["remote", "get-url", "origin"])
    assert (ran, out) == (False, None)
    assert seen["timeout"] == grouping.GIT_TIMEOUT_S


@pytest.mark.skipif(GIT is None, reason="git is not installed")
def test_probe_reads_a_real_repository(tmp_path):
    repo = tmp_path / "widget"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["remote", "add", "origin", "git@github.com:acme/widget.git"],
    ):
        subprocess.run([GIT, "-C", str(repo), *args], check=True, capture_output=True)

    p = grouping.probe_path(str(repo))

    assert p.exists and p.git_ran
    assert p.remote == "git@github.com:acme/widget.git"
    assert p.common_dir and p.common_dir.rstrip("/").endswith(".git")
    assert grouping.normalize_remote(p.remote) == "https://github.com/acme/widget"


# ------------------------------------------------------------ manual layer --


def test_find_projects_matches_name_or_path_case_insensitively(conn):
    add_project(conn, "/w/atlas-chat")
    add_project(conn, "/w/other/frontend")

    assert [r["root_path"] for r in grouping.find_projects(conn, "chatforen")] == [
        "/w/atlas-chat"
    ]
    assert [r["root_path"] for r in grouping.find_projects(conn, "OTHER/")] == [
        "/w/other/frontend"
    ]


def test_an_exact_name_beats_the_substring_haze(conn):
    add_project(conn, "/w/atlas-chat")
    add_project(conn, "/w/atlas-chat-loam")
    add_project(conn, "/w/atlas-chat/backend")

    picked = grouping.resolve_project(conn, "atlas-chat")
    assert picked["root_path"] == "/w/atlas-chat"


def test_ambiguous_match_lists_the_candidates_and_refuses_to_guess(conn):
    add_project(conn, "/w/one/backend")
    add_project(conn, "/w/two/backend")

    with pytest.raises(grouping.AmbiguousMatch) as e:
        grouping.resolve_project(conn, "backend")
    assert sorted(e.value.candidates) == ["/w/one/backend", "/w/two/backend"]


def test_set_creates_the_group_when_it_is_new(conn):
    pid = add_project(conn, "/w/thing")
    gid, created = grouping.group_for_name(conn, "Fresh")
    assert created
    grouping.pin_projects(conn, [pid], gid)
    assert group_names(conn)["/w/thing"] == "Fresh"
    assert conn.execute("SELECT origin FROM project_group").fetchone()[0] == "manual"


def test_unset_returns_a_project_to_detection(conn):
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    stray = add_project(conn, "/w/repo/sub", remote="https://github.com/o/repo.git")
    gid, _ = grouping.group_for_name(conn, "Parked")
    grouping.pin_projects(conn, [stray], gid)

    grouping.unpin_projects(conn, [stray])
    row = conn.execute("SELECT * FROM project WHERE project_id = ?", (stray,)).fetchone()
    assert (row["group_id"], row["group_pinned"]) == (None, 0)

    grouping.detect(conn, probe_fs=False)
    assert group_names(conn)["/w/repo/sub"] == "repo"


def test_group_names_do_not_collide(conn):
    grouping.create_group(conn, "Dup")
    with pytest.raises(grouping.GroupError):
        grouping.create_group(conn, "Dup")
    g = grouping.resolve_group(conn, "Dup")
    grouping.create_group(conn, "Other")
    with pytest.raises(grouping.GroupError):
        grouping.rename_group(conn, g["group_id"], "Other")


# -------------------------------------------------------------- read model --


def test_list_groups_sums_hours_and_shows_ungrouped_as_a_group_of_one(conn):
    a = add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    b = add_project(conn, "/w/repo/sub", remote="https://github.com/o/repo.git")
    c = add_project(conn, "/w/lonely")
    add_time(conn, a, 2.0)
    add_time(conn, b, 1.0)
    add_time(conn, c, 0.5)

    grouping.detect(conn, probe_fs=False)
    views = grouping.list_groups(conn)

    assert [v.name for v in views] == ["repo", "lonely"]
    assert views[0].hours == pytest.approx(3.0)
    assert views[0].web_url == "https://github.com/o/repo"
    assert len(views[0].members) == 2
    assert views[1].group_id is None and len(views[1].members) == 1


# --------------------------------------------------------------------- cli --


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    """A migrated database behind a config dir, plus a `run` helper."""
    monkeypatch.setattr(grouping, "probe_path", lambda p: grouping.Probe(exists=False))
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    from cc_insights import db

    def run(*argv):
        return cli.main(["--config-dir", str(tmp_path), *argv])

    conn = db.connect(tmp_path / "cc-insights.db")
    try:
        yield conn, run
    finally:
        conn.close()


def test_cli_group_auto_then_list(cli_db, capsys):
    conn, run = cli_db
    a = add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    add_project(conn, "/w/repo/.claude/worktrees/wt")
    add_time(conn, a, 4.0)
    capsys.readouterr()

    assert run("group", "auto") == 0
    out = capsys.readouterr().out
    assert "created 1 group(s)" in out
    assert "pinned (skipped)" in out

    assert run("group", "list") == 0
    out = capsys.readouterr().out
    assert "repo" in out
    assert "2 paths" in out
    assert "https://github.com/o/repo" in out
    assert "4.0 h" in out


def test_cli_group_list_puts_an_ungrouped_path_on_one_line(cli_db, capsys):
    """A group of one with no group: the header already says everything."""
    conn, run = cli_db
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    add_project(conn, "/w/loose")
    capsys.readouterr()

    run("group", "auto")
    run("group", "list")
    out = capsys.readouterr().out

    assert "1 ungrouped" in out
    line = next(ln for ln in out.splitlines() if "loose" in ln)
    assert "ungrouped  /w/loose" in line
    assert out.count("/w/loose") == 1   # the header no longer repeats itself


def test_cli_group_auto_dry_run_writes_nothing(cli_db, capsys):
    conn, run = cli_db
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    capsys.readouterr()

    assert run("group", "auto", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "nothing was written" in out
    assert "would create 1 group(s) and place 1 project(s)" in out
    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0
    assert conn.execute("SELECT group_id FROM project").fetchone()[0] is None


def test_cli_group_set_is_ambiguity_safe(cli_db, capsys):
    conn, run = cli_db
    add_project(conn, "/w/one/backend")
    add_project(conn, "/w/two/backend")
    capsys.readouterr()

    assert run("group", "set", "backend", "--to", "Nope") == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "/w/one/backend" in err and "/w/two/backend" in err
    # nothing was created on the way out
    assert conn.execute("SELECT count(*) FROM project_group").fetchone()[0] == 0


def test_cli_group_set_unset_new_rename(cli_db, capsys):
    conn, run = cli_db
    add_project(conn, "/w/alpha")
    add_project(conn, "/w/beta")
    capsys.readouterr()

    assert run("group", "new", "Bucket") == 0
    assert run("group", "set", "alpha", "beta", "--to", "Bucket") == 0
    assert capsys.readouterr().out.count("pinned") == 2
    assert group_names(conn) == {"/w/alpha": "Bucket", "/w/beta": "Bucket"}

    assert run("group", "rename", "Bucket", "Pail") == 0
    capsys.readouterr()
    assert set(group_names(conn).values()) == {"Pail"}

    assert run("group", "unset", "alpha") == 0
    capsys.readouterr()
    assert group_names(conn) == {"/w/beta": "Pail"}


def test_cli_group_set_pins_survive_auto(cli_db, capsys):
    conn, run = cli_db
    add_project(conn, "/w/repo", remote="https://github.com/o/repo.git")
    add_project(conn, "/w/repo/sub", remote="https://github.com/o/repo.git")
    capsys.readouterr()

    assert run("group", "set", "/w/repo/sub", "--to", "Elsewhere") == 0
    assert run("group", "auto") == 0
    assert "1 pinned (skipped)" in capsys.readouterr().out
    assert group_names(conn)["/w/repo/sub"] == "Elsewhere"


def test_cli_group_needs_the_migration(tmp_path, capsys, monkeypatch):
    """A database from before WP13 should say so, not raise sqlite3.OperationalError."""
    from cc_insights import db

    original = db.discover_migrations
    monkeypatch.setattr(
        db, "discover_migrations",
        lambda directory=None: [m for m in original(directory) if m[0] < 2],
    )
    assert cli.main(["--config-dir", str(tmp_path), "init"]) == 0
    monkeypatch.setattr(db, "discover_migrations", original)
    capsys.readouterr()

    with pytest.raises(SystemExit):
        cli.main(["--config-dir", str(tmp_path), "group", "list"])
    # _open_db's generic schema guard answers this for every command and every
    # future migration, so there is one message rather than a per-feature one.
    err = capsys.readouterr().err
    assert "run `cci init` to migrate" in err
