"""`cci team`: the pure helpers, and what the commands refuse to guess.

The end-to-end half lives in `test_remote_e2e.py`, against a real server.
What is here is the part that has no server in it: duration parsing, the
"which team did you mean" resolution, and the rendering rules that exist for
a safety reason rather than a cosmetic one.

The resolution tests matter more than they look. Every one of these commands
changes who can read somebody else's agent time, so a command that resolves
an ambiguous argument by picking the first match is a command that can share
the wrong repo with the wrong team and say it succeeded.
"""

from __future__ import annotations

import pytest

from cc_insights import cli


# --------------------------------------------------------------------------
# --expires-in
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spec,ms", [
    ("1h", 3_600_000),
    ("12h", 12 * 3_600_000),
    ("3d", 3 * 86_400_000),
    ("1w", 7 * 86_400_000),
    ("2W", 14 * 86_400_000),
    (" 6h ", 6 * 3_600_000),
])
def test_a_duration_parses_to_milliseconds(spec, ms):
    assert cli._duration_ms(spec) == ms


@pytest.mark.parametrize("spec", ["7", "", "d", "0d", "-3d", "3days", "3m", "abc", "1.5d"])
def test_a_duration_without_a_unit_is_refused_rather_than_assumed(spec):
    """"7" could be hours or days, and the two differ by a factor of 24.

    Guessing wrong in the short direction is an annoyance -- the colleague
    asks for another code. Guessing wrong in the long direction leaves a live
    credential in a chat log for a week. So neither is guessed.

    Minutes are absent from the units on purpose: a code that lives for
    minutes is one the recipient will miss, and the failure is silent, since
    an expired code is deliberately indistinguishable from a wrong one.
    """
    with pytest.raises(ValueError):
        cli._duration_ms(spec)


# --------------------------------------------------------------------------
# "in 2 days" / "3 hours ago"
# --------------------------------------------------------------------------


NOW = 1_800_000_000_000


@pytest.mark.parametrize("at,expected", [
    (NOW + 2 * 86_400_000, "in 2 days"),
    (NOW + 86_400_000, "in 1 day"),
    (NOW + 3 * 3_600_000, "in 3 hours"),
    (NOW + 90_000, "in 1 minute"),
    (NOW + 1_000, "in under a minute"),
    (NOW - 3 * 3_600_000, "3 hours ago"),
    (NOW - 5 * 86_400_000, "5 days ago"),
    (None, "—"),
    (0, "—"),
])
def test_relative_times_read_the_way_the_decision_is_made(at, expected):
    """"can I still send this to somebody" is answered by "in 2 days".

    An absolute epoch-ms timestamp is the wrong answer to that question: it
    makes the reader do arithmetic across a timezone to find out whether a
    credential is still live.
    """
    assert cli._relative(at, now=NOW) == expected


# --------------------------------------------------------------------------
# which team did you mean
# --------------------------------------------------------------------------


class FakeClient:
    def __init__(self, teams):
        self._teams = teams

    def teams(self):
        return self._teams


PLATFORM = {"teamId": "tm_aaa", "name": "Platform", "role": "admin"}
DESIGN = {"teamId": "tm_bbb", "name": "Design", "role": "member"}
PLATFORM_TWO = {"teamId": "tm_ccc", "name": "Platform Tooling", "role": "admin"}


def test_one_team_needs_no_flag():
    """docs/ACCOUNTS.md §1 is "one private instance, for one team".

    So the single-team case is the common one and it gets the wordless path.
    """
    assert cli._resolve_team(FakeClient([PLATFORM]), None) == PLATFORM


def test_several_teams_and_no_flag_refuses_and_lists_them(capsys):
    """It must not pick one. `cci team share` with no `--team` on a person
    who is on three teams would otherwise share a repo with whichever team
    sorted first, and report success."""
    with pytest.raises(SystemExit) as exc:
        cli._resolve_team(FakeClient([PLATFORM, DESIGN]), None)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "--team" in err and "Platform" in err and "Design" in err


def test_a_team_resolves_by_id_or_by_name():
    client = FakeClient([PLATFORM, DESIGN])
    assert cli._resolve_team(client, "tm_bbb") == DESIGN
    assert cli._resolve_team(client, "Design") == DESIGN
    assert cli._resolve_team(client, "desi") == DESIGN, "a prefix should work"
    assert cli._resolve_team(client, "DESIGN") == DESIGN, "case should not matter"


def test_an_exact_id_wins_over_a_name_substring():
    """Otherwise a team whose NAME contains another team's id is a trap."""
    odd = {"teamId": "tm_zzz", "name": "contains tm_aaa in the name", "role": "admin"}
    assert cli._resolve_team(FakeClient([PLATFORM, odd]), "tm_aaa") == PLATFORM


def test_an_ambiguous_name_refuses_and_shows_the_candidates(capsys):
    with pytest.raises(SystemExit):
        cli._resolve_team(FakeClient([PLATFORM, PLATFORM_TWO]), "Platform")
    err = capsys.readouterr().err
    assert "matches 2" in err
    assert "Platform" in err and "Platform Tooling" in err


def test_no_teams_at_all_names_both_ways_out(capsys):
    """"you are not on a team" is useless without the two commands that fix it,
    and which one is right depends on something the tool cannot know."""
    with pytest.raises(SystemExit):
        cli._resolve_team(FakeClient([]), None)
    err = capsys.readouterr().err
    assert "cci team new" in err and "cci team join" in err


# --------------------------------------------------------------------------
# which repo did you mean
# --------------------------------------------------------------------------


LOAM = {"repoId": "a" * 64, "name": "harbor-cli",
        "remoteUrl": "https://github.com/acme/loam", "via": ["github"]}
GLOSSA = {"repoId": "b" * 64, "name": "glyphwright",
          "remoteUrl": "https://github.com/acme/glossa", "via": ["published"]}
LOAMY = {"repoId": "c" * 64, "name": "harbor-cli Tools",
         "remoteUrl": "https://github.com/acme/loam-tools", "via": ["team:tm_aaa"]}


def test_a_repo_resolves_by_id_name_or_remote():
    repos = [LOAM, GLOSSA]
    assert cli._resolve_repo(repos, "a" * 64, "in scope") == LOAM
    assert cli._resolve_repo(repos, "harbor-cli", "in scope") == LOAM
    assert cli._resolve_repo(repos, "glossa", "in scope") == GLOSSA
    assert cli._resolve_repo(repos, "acme/glossa", "in scope") == GLOSSA


def test_an_ambiguous_repo_is_never_resolved_by_picking_one(capsys):
    """`cci team share loam` with two matching repos must not share either.

    Sharing the wrong repo puts somebody's work in front of a team that was
    never meant to see it, and the person who ran the command would be told
    it worked.
    """
    with pytest.raises(SystemExit) as exc:
        cli._resolve_repo([LOAM, LOAMY], "loam", "in scope")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "matches 2 repos" in err
    assert "harbor-cli" in err and "harbor-cli Tools" in err


def test_an_exact_repo_id_beats_a_substring_match():
    assert cli._resolve_repo([LOAM, LOAMY], "a" * 64, "in scope") == LOAM


def test_an_unknown_repo_says_which_list_was_searched(capsys):
    """"not in what you can see" and "not on this team's roster" are different
    problems with different next commands."""
    with pytest.raises(SystemExit):
        cli._resolve_repo([LOAM], "nope", "on Platform's roster")
    assert "on Platform's roster" in capsys.readouterr().err


# --------------------------------------------------------------------------
# who did you mean
# --------------------------------------------------------------------------


ALICE = {"accountId": "acc_1", "actor": "alice", "role": "admin"}
BOB = {"accountId": "acc_2", "actor": "bob", "role": "member"}
BOBBY = {"accountId": "acc_3", "actor": "bobby", "role": "member"}


def test_a_member_resolves_by_actor_or_account_id():
    assert cli._resolve_member([ALICE, BOB], "bob") == BOB
    assert cli._resolve_member([ALICE, BOB], "acc_1") == ALICE
    assert cli._resolve_member([ALICE, BOB], "ALICE") == ALICE


def test_an_exact_actor_wins_over_a_longer_one():
    """`cci team remove bob` must remove bob, not bobby.

    A substring rule alone would make the shorter of two names unusable, and
    the failure removes the wrong person from a team.
    """
    assert cli._resolve_member([BOB, BOBBY], "bob") == BOB
    assert cli._resolve_member([BOB, BOBBY], "bobby") == BOBBY


def test_an_ambiguous_person_refuses(capsys):
    carol = {"accountId": "acc_4", "actor": "carol-b", "role": "member"}
    carla = {"accountId": "acc_5", "actor": "carol-c", "role": "member"}
    with pytest.raises(SystemExit):
        cli._resolve_member([carol, carla], "carol")
    err = capsys.readouterr().err
    assert "carol-b" in err and "carol-c" in err


# --------------------------------------------------------------------------
# the parser
# --------------------------------------------------------------------------


def test_every_team_verb_is_reachable_and_names_its_function():
    """A subcommand wired to no function exits with a traceback, not a message.

    Enumerated rather than spot-checked for the reason `scope.py` gives about
    the team router: a verb added next month is covered the day it is
    written.
    """
    parser = cli.build_parser()
    verbs = ["list", "members", "invites", "repos", "sessions", "actors", "daily"]
    for verb in verbs:
        args = parser.parse_args(["team", verb])
        assert callable(getattr(args, "fn", None)), verb

    with_args = [
        (["team", "new", "Platform"], "name", "Platform"),
        (["team", "join", "ccij_abc"], "code", "ccij_abc"),
        (["team", "revoke", "inv_x"], "invite_id", "inv_x"),
        (["team", "remove", "bob"], "who", "bob"),
        (["team", "share", "harbor-cli"], "repo", "harbor-cli"),
        (["team", "unshare", "harbor-cli"], "repo", "harbor-cli"),
        (["team", "branches", "off", "harbor-cli"], "state", "off"),
    ]
    for argv, attr, value in with_args:
        args = parser.parse_args(argv)
        assert callable(getattr(args, "fn", None)), argv
        assert getattr(args, attr) == value


def test_the_bare_team_command_still_works_without_a_subcommand():
    """`cci team` on its own is the useful default and must stay that way.

    Making somebody pick a subcommand to see the obvious thing is a worse
    first run, and this is the one command a new user is told to type.
    """
    args = cli.build_parser().parse_args(["team"])
    assert args.fn is cli.cmd_team


def test_the_verbs_that_act_on_a_team_all_accept_team():
    """Added by one helper so that no verb can be written without it."""
    parser = cli.build_parser()
    for verb in ("members", "invite", "invites", "repos", "share", "unshare",
                 "branches", "leave", "remove"):
        argv = ["team", verb, "--team", "Platform"]
        argv += {"share": ["x"], "unshare": ["x"], "remove": ["x"],
                 "branches": ["on", "x"]}.get(verb, [])
        assert parser.parse_args(argv).team == "Platform", verb


def test_branches_only_accepts_on_or_off(capsys):
    """A third value would have to mean something, and there is no third state."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["team", "branches", "maybe", "harbor-cli"])


def test_an_invite_defaults_to_a_member_seat_and_no_overrides():
    """The CLI sends nothing it was not asked to, so the server's defaults --
    single use, three days -- are what apply."""
    args = cli.build_parser().parse_args(["team", "invite"])
    assert args.role == "member"
    assert args.expires_in is None and args.uses is None and args.note is None
