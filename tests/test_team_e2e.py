"""`cci team`, end to end, against a real server and two real accounts.

`test_team_commands.py` covers the parts with no server in them.
`server/tests/test_join_codes.py` attacks the credential at the HTTP layer.
This file is the join between them: two config directories, two device-flow
sign-ins, and the commands two colleagues would actually type.

It is worth having as well as the other two, because the way "the code is
shown once" dies in practice is not a server change. It is a CLI that caches
the code in the config directory so an admin can look it up again -- which
no server test would ever see, and which `test_the_join_code_is_never_shown_
again_by_any_command` below checks for by reading every file the commands
wrote.

Fixtures come from `test_remote_e2e` rather than being copied: `live` boots a
real uvicorn and is module-scoped there for a reason, and two copies of a
server harness is two things to keep in step.
"""

from __future__ import annotations

from test_remote_e2e import (  # noqa: F401  (imported for pytest to collect)
    ACTOR,
    ROOT_PATH,
    _login_via_cli,
    _make_db,
    clean,
    home,
    live,
    local,
)


def _second_home(tmp_path, monkeypatch, name: str):
    """A second config directory, for the colleague being invited.

    Two homes rather than two tokens against one, because the thing under
    test is two PEOPLE: the admin mints with their credential and the joiner
    redeems with theirs. A test that used one config directory for both would
    exercise neither half of the property that makes this consent.
    """
    where = tmp_path / name
    where.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CC_INSIGHTS_HOME", str(where))
    return where


def _code_from(out: str) -> str:
    return next(w for w in out.split() if w.startswith("ccij_"))


def _setup_alice(live, home, capsys, *, team="Platform"):
    from cc_insights import cli

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    assert cli.main(["--config-dir", str(home), "team", "new", team]) == 0
    capsys.readouterr()


def _setup_bob(live, tmp_path, monkeypatch, capsys):
    from cc_insights import cli

    bob_home = _second_home(tmp_path, monkeypatch, "bob-home")
    assert cli.main(["--config-dir", str(bob_home), "init"]) == 0
    assert _login_via_cli(live, bob_home, actor="bob", repos=()) == 0
    capsys.readouterr()
    return bob_home


def test_an_admin_mints_a_code_and_a_colleague_joins_with_it(
    live, home, tmp_path, monkeypatch, capsys
):
    """The whole consent flow, as two people would run it.

    What this demonstrates is not that it works but that it takes TWO
    credentials. The code alone puts nobody on a roster, and Alice never
    learns or types Bob's account id -- which she could not have obtained
    anyway, because there is deliberately no way to look one up.
    """
    from cc_insights import cli

    _setup_alice(live, home, capsys)

    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    minted = capsys.readouterr().out
    assert "COPY IT NOW, IT IS NOT SHOWN AGAIN" in minted
    code = _code_from(minted)
    assert len(code) > 40, "a short code would be guessable with no throttle"
    # On a line of its own, so a double-click selects all of it and no more.
    assert any(line.strip() == code for line in minted.splitlines())
    assert "the way you would send a password" in minted

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    joined = capsys.readouterr().out
    assert "joined Platform as member" in joined
    # Said out loud, because the natural assumption on joining is that your
    # own work is now on the team. It is not.
    assert "did not share any of your own work" in joined

    assert cli.main(["--config-dir", str(bob_home), "team", "list"]) == 0
    assert "Platform" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(home), "team", "members"]) == 0
    members = capsys.readouterr().out
    assert "bob" in members and "invited by alice" in members
    assert "created the team" in members, "Alice's own row has no inviter"


def test_the_join_code_is_never_shown_again_by_any_command(
    live, home, tmp_path, monkeypatch, capsys
):
    """It is hashed on the server, so no command can reproduce it.

    The last assertion is the one worth having here rather than on the
    server: it reads every file the commands wrote, because a CLI that
    cached the code locally "so the admin can look it up" would pass every
    server test in the suite.
    """
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "invite",
                     "--note", "for the contractors"]) == 0
    code = _code_from(capsys.readouterr().out)

    assert cli.main(["--config-dir", str(home), "team", "invites"]) == 0
    listed = capsys.readouterr().out
    assert code not in listed, "the listing re-showed a live join code"
    assert "for the contractors" in listed and "minted by alice" in listed
    assert "stored hashed and cannot be recovered" in listed

    assert cli.main(["--config-dir", str(home), "team", "members"]) == 0
    assert code not in capsys.readouterr().out

    for path in home.rglob("*"):
        if path.is_file():
            try:
                assert code not in path.read_text(errors="ignore"), path
            except (UnicodeDecodeError, PermissionError, OSError):
                pass


def test_a_revoked_code_stops_working_and_the_refusal_says_nothing(
    live, home, tmp_path, monkeypatch, capsys
):
    """The admin pasted it in the wrong channel. Revocation has to be enough.

    And the refusal Bob sees must not say "revoked": an invalid code and a
    revoked one are one answer, because telling them apart confirms the code
    was real to somebody who has just shown they were not invited.
    """
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    out = capsys.readouterr().out
    code = _code_from(out)
    invite_id = next(line.split()[1] for line in out.splitlines()
                     if line.strip().startswith("id "))

    assert cli.main(["--config-dir", str(home), "team", "revoke", invite_id]) == 0
    assert "revoked" in capsys.readouterr().out

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 1
    captured = capsys.readouterr()
    refusal = captured.out + captured.err
    for word in ("revoked", "expired", "used up", "Platform"):
        assert word not in refusal, f"the refusal disclosed {word!r}"

    assert cli.main(["--config-dir", str(bob_home), "team", "list"]) == 0
    assert "not on a team" in capsys.readouterr().out


def test_a_single_use_code_does_not_admit_a_second_person(
    live, home, tmp_path, monkeypatch, capsys
):
    """The default, observed from the far end: one colleague, then nobody."""
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    minted = capsys.readouterr().out
    assert "good for   1 person" in minted
    code = _code_from(minted)

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    capsys.readouterr()

    carol_home = _second_home(tmp_path, monkeypatch, "carol-home")
    assert cli.main(["--config-dir", str(carol_home), "init"]) == 0
    assert _login_via_cli(live, carol_home, actor="carol", repos=()) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(carol_home), "team", "join", code]) == 1
    assert cli.main(["--config-dir", str(carol_home), "team", "list"]) == 0
    assert "not on a team" in capsys.readouterr().out


def test_sharing_a_repo_says_which_of_the_two_grants_it_made(
    live, home, local, tmp_path, monkeypatch, capsys
):
    """An admin who thinks they shared a repo and shared one row is misled.

    `docs/SERVER_API.md` §4.5: a roster delegates the adder's access. Alice
    signed in with GitHub access to `alice/harbor-cli`, so this is the full grant
    -- and the output has to distinguish that from the publisher-level case,
    because the two look identical at the command line and differ entirely
    in what the team can then read.
    """
    from cc_insights import cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="j").close()
    assert cli.main(["--config-dir", str(home), "publish", "--yes"]) == 0
    assert cli.main(["--config-dir", str(home), "team", "new", "Platform"]) == 0
    capsys.readouterr()

    assert cli.main(["--config-dir", str(home), "team", "repos", "--ids"]) == 0
    assert "harbor-cli" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(home), "team", "share", "harbor-cli"]) == 0
    shared = capsys.readouterr().out
    assert "shared harbor-cli with Platform" in shared
    assert "every published row in this repo" in shared, shared

    assert cli.main(["--config-dir", str(home), "team", "repos",
                     "--team", "Platform"]) == 0
    assert "Platform SHARES" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(home), "team", "branches", "off",
                     "harbor-cli"]) == 0
    off = capsys.readouterr().out
    assert "branch names hidden from Platform" in off
    assert "already published" in off, "the retroactive half must be said"

    assert cli.main(["--config-dir", str(home), "team", "unshare", "harbor-cli"]) == 0
    assert "no longer sees harbor-cli" in capsys.readouterr().out


def test_joining_shows_the_new_member_nothing_that_was_not_rostered(
    live, home, local, tmp_path, monkeypatch, capsys
):
    """The disclosure rule, seen from the command line rather than the API.

    Alice has published real data. Bob joins her team, which shares nothing,
    and `cci team` must show him an empty scope -- because membership is not
    a grant and a roster is the only thing that shares a repo. Then Alice
    rosters it, and only then does he see it.
    """
    from cc_insights import cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="k").close()
    assert cli.main(["--config-dir", str(home), "publish", "--yes"]) == 0
    assert cli.main(["--config-dir", str(home), "team", "new", "Platform"]) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    code = _code_from(capsys.readouterr().out)

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    assert "shares no repos yet" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(bob_home), "team"]) == 0
    seen = capsys.readouterr().out
    assert "no repos in scope" in seen, seen
    assert "harbor-cli" not in seen, "joining disclosed a repo nobody rostered"
    assert ROOT_PATH not in seen

    assert cli.main(["--config-dir", str(home), "team", "share", "harbor-cli"]) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(bob_home), "team"]) == 0
    now = capsys.readouterr().out
    assert "harbor-cli" in now
    # Still never a path: the team store never received one.
    assert ROOT_PATH not in now


def test_leaving_a_team_takes_the_view_away_again(
    live, home, local, tmp_path, monkeypatch, capsys
):
    """Consent that cannot be withdrawn is not much of a consent model."""
    from cc_insights import cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="m").close()
    assert cli.main(["--config-dir", str(home), "publish", "--yes"]) == 0
    assert cli.main(["--config-dir", str(home), "team", "new", "Platform"]) == 0
    assert cli.main(["--config-dir", str(home), "team", "share", "harbor-cli"]) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    code = _code_from(capsys.readouterr().out)

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    capsys.readouterr()
    assert cli.main(["--config-dir", str(bob_home), "team"]) == 0
    assert "harbor-cli" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(bob_home), "team", "leave", "--yes"]) == 0
    assert "left Platform" in capsys.readouterr().out
    assert cli.main(["--config-dir", str(bob_home), "team"]) == 0
    assert "harbor-cli" not in capsys.readouterr().out


def test_the_last_admin_cannot_leave_and_is_told_why(live, home, capsys):
    """A team with no admin is a roster nobody can correct."""
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "leave", "--yes"]) == 1
    captured = capsys.readouterr()
    assert "admin" in (captured.out + captured.err).lower()
    assert cli.main(["--config-dir", str(home), "team", "list"]) == 0
    assert "Platform" in capsys.readouterr().out


def test_an_admin_can_remove_somebody_and_is_pointed_at_the_codes(
    live, home, tmp_path, monkeypatch, capsys
):
    """Removal alone is not enough if a live multi-use code readmits them.

    So the command says so. A spent single-use code cannot let them back in
    -- the server spends a seat per join, not per person -- but a code with
    seats left can, and an admin doing this deliberately should be told
    where to look.
    """
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "invite", "--uses", "3"]) == 0
    code = _code_from(capsys.readouterr().out)

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    capsys.readouterr()

    assert cli.main(["--config-dir", str(home), "team", "remove", "bob"]) == 0
    removed = capsys.readouterr().out
    assert "removed bob from Platform" in removed
    assert "cci team invites" in removed

    assert cli.main(["--config-dir", str(bob_home), "team", "list"]) == 0
    assert "not on a team" in capsys.readouterr().out


def test_a_member_cannot_mint_codes_or_roster_repos(
    live, home, tmp_path, monkeypatch, capsys
):
    """Joining does not hand over the actions that widen what a team sees.

    `share` is the only command in the system that changes what OTHER people
    can read, so what a plain member may do is a security question rather
    than a matter of convenience.
    """
    from cc_insights import cli

    _setup_alice(live, home, capsys)
    assert cli.main(["--config-dir", str(home), "team", "invite"]) == 0
    code = _code_from(capsys.readouterr().out)

    bob_home = _setup_bob(live, tmp_path, monkeypatch, capsys)
    assert cli.main(["--config-dir", str(bob_home), "team", "join", code]) == 0
    capsys.readouterr()

    assert cli.main(["--config-dir", str(bob_home), "team", "invite"]) == 1
    captured = capsys.readouterr()
    assert "admin" in (captured.out + captured.err).lower()

    # He can still read the roster he is on; that is what membership is for.
    assert cli.main(["--config-dir", str(bob_home), "team", "members"]) == 0
    assert "alice" in capsys.readouterr().out


def test_daily_and_actors_both_show_the_withheld_hours(
    live, home, local, capsys
):
    """docs/ACCOUNTS.md §5 rule 3: withheld time is counted, never omitted.

    `/daily` is the endpoint that most needs the block, and the contract
    records that it shipped without one. `remote.team_daily` used to return
    `body["days"]` and drop it, which recreated the same failure in the
    client -- a "hours this week" chart built by summing `days` with the
    unpublishable part silently missing, and no way for the reader to tell a
    quiet week from a week spent in a repo with no remote.
    """
    from cc_insights import cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="w").close()
    assert cli.main(["--config-dir", str(home), "publish", "--yes"]) == 0
    capsys.readouterr()

    for verb in ("daily", "actors"):
        assert cli.main(["--config-dir", str(home), "team", verb]) == 0
        out = capsys.readouterr().out
        assert "WITHHELD" in out, f"`cci team {verb}` dropped the withheld block"
        assert "work in no repo at all" in out

    assert cli.main(["--config-dir", str(home), "team", "daily"]) == 0
    daily = capsys.readouterr().out
    # The axis is labelled, because §4.8 buckets by UTC and a team spans
    # timezones -- a renderer that does not say so shows somebody a day that
    # is not theirs.
    assert "UTC" in daily
