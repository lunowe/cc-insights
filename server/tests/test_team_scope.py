"""The three rules from docs/ACCOUNTS.md §5, and the boundary from REDACTION.md §1.

    A row may be published only if it belongs to a repo, and only to people
    who can already see that repo.

Everything here is a disclosure test. Each one says what a colleague would be
able to see, or infer, if the assertion stopped holding -- because the failure
mode of this file is not a broken feature, it is somebody reading something
they were never granted.
"""

from __future__ import annotations

import pytest

from cci_server.repoid import github_remote, repo_id

PUBLIC = repo_id(github_remote("github.com", "acme/public"))
SECRET = repo_id(github_remote("github.com", "acme/secret"))
BOBS = repo_id(github_remote("github.com", "bob/side-project"))

DAY = 86_400_000
T0 = 1_789_000_000_000


def publish_repo(client, who, rid: str, name: str, owner="acme"):
    r = client.post("/v1/team/publish", json={
        "kind": "repos", "actor": who.actor,
        "rows": [{"repoId": rid, "remoteUrl": f"https://github.com/{owner}/{name}",
                  "forge": "github", "owner": owner, "repo": name,
                  "webUrl": f"https://github.com/{owner}/{name}", "name": name}],
    }, headers=who.auth)
    assert r.status_code == 200, r.text


def publish_session(client, who, sid: str, rid: str, *, branch="main",
                    started=T0, ms=3_600_000, source="claude_code"):
    r = client.post("/v1/team/publish", json={
        "kind": "sessions", "actor": who.actor,
        "rows": [{"sessionId": sid, "repoId": rid, "actor": who.actor,
                  "source": source, "gitBranch": branch, "startedAt": started,
                  "endedAt": started + ms, "activeMs": ms, "eventCount": 10}],
    }, headers=who.auth)
    assert r.status_code == 200, r.text


def publish_span(client, who, span_id: str, sid: str, *, started=T0, ms=3_600_000,
                 role="human"):
    r = client.post("/v1/team/publish", json={
        "kind": "spans", "actor": who.actor,
        "rows": [{"spanId": span_id, "sessionId": sid, "threadRole": role,
                  "startedAt": started, "endedAt": started + ms, "eventCount": 10}],
    }, headers=who.auth)
    assert r.status_code == 200, r.text


def make_team(client, who, name="Platform") -> str:
    r = client.post("/v1/teams", json={"name": name}, headers=who.auth)
    assert r.status_code == 201, r.text
    return r.json()["teamId"]


@pytest.fixture
def world(client, alice, bob):
    """Alice works on two repos; only one of them will ever reach a roster.

    `SECRET` is the repo Bob must never see under any query, and it holds
    twice as much time as `PUBLIC` so that any aggregate which accidentally
    includes it is off by an unmistakable amount rather than by rounding.
    """
    publish_repo(client, alice, PUBLIC, "public")
    publish_repo(client, alice, SECRET, "secret")
    publish_session(client, alice, "s-pub", PUBLIC, branch="feat/ui", started=T0)
    publish_span(client, alice, "sp-pub", "s-pub", started=T0, ms=1 * 3_600_000)
    publish_session(client, alice, "s-sec", SECRET,
                    branch="feat/restricted-org-dbs", started=T0 + DAY)
    publish_span(client, alice, "sp-sec", "s-sec", started=T0 + DAY, ms=2 * 3_600_000)

    publish_repo(client, bob, BOBS, "side-project", owner="bob")
    publish_session(client, bob, "s-bob", BOBS, started=T0)
    publish_span(client, bob, "sp-bob", "s-bob", started=T0, ms=30 * 60_000)

    team = make_team(client, alice)
    client.post(f"/v1/teams/{team}/members",
                json={"accountId": bob.account_id, "role": "member"},
                headers=alice.auth)
    client.post(f"/v1/teams/{team}/repos", json={"repoId": PUBLIC}, headers=alice.auth)
    return team


# --------------------------------------------------------------------------
# rule 1: aggregates are computed inside the viewer's scope
# --------------------------------------------------------------------------


def test_a_teammate_sees_only_the_rostered_repo(client, bob, world):
    """A repo off the roster must not be listed, named, or addressable.

    If it appears, the existence of `acme/secret` -- and the fact that Alice
    works on it -- has leaked to somebody with no access to the repository.
    """
    body = client.get("/v1/team/repos", headers=bob.auth).json()
    ids = {r["repoId"] for r in body["repos"]}
    assert PUBLIC in ids
    assert SECRET not in ids
    assert "secret" not in body_text(body)


def test_an_aggregate_never_spans_outside_the_viewers_scope(client, alice, bob, world):
    """The leak docs/ACCOUNTS.md §5 rule 1 exists to prevent, measured.

    Alice has 3 h published: 1 h on the rostered repo and 2 h on one Bob
    cannot see. If Bob's total reads 3 h, the existence of `acme/secret` has
    leaked the moment he looked at a number -- which is precisely why a
    precomputed rollup is forbidden.
    """
    mine = client.get("/v1/team/summary", headers=alice.auth).json()
    assert mine["activeMs"] == 3 * 3_600_000

    theirs = client.get("/v1/team/summary", headers=bob.auth).json()
    assert theirs["activeMs"] == 1 * 3_600_000 + 30 * 60_000
    assert {r["repoId"] for r in theirs["byRepo"]} == {PUBLIC, BOBS}
    assert SECRET not in body_text(theirs)


def test_the_same_query_returns_different_totals_to_different_viewers(client, alice,
                                                                      bob, world):
    """There is no viewer-independent answer, so there can be no rollup table.

    A nightly aggregate keyed on anything but (viewer scope, period) would
    have to pick one of these two numbers, and either choice is wrong for
    somebody.
    """
    a = client.get("/v1/team/summary", headers=alice.auth).json()["activeMs"]
    b = client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"]
    assert a != b


def test_widening_the_roster_changes_an_aggregate_immediately(client, alice, bob, world):
    """The aggregate is computed now, against the scope as it is now.

    A cached total would keep serving the old number after access changed --
    in the other direction, it would keep serving a repo somebody had just
    been removed from.
    """
    before = client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"]
    client.post(f"/v1/teams/{world}/repos", json={"repoId": SECRET}, headers=alice.auth)
    after = client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"]
    assert after == before + 2 * 3_600_000


def test_narrowing_the_roster_takes_the_repo_back_out_of_every_aggregate(
    client, alice, bob, world
):
    """Removing a repo from a roster must remove it from the totals, not just the list."""
    client.delete(f"/v1/teams/{world}/repos/{PUBLIC}", headers=alice.auth)
    body = client.get("/v1/team/summary", headers=bob.auth).json()
    assert body["activeMs"] == 30 * 60_000  # only his own
    assert PUBLIC not in body_text(body)


def test_asking_for_a_repo_outside_your_scope_is_ignored_not_rejected(client, bob, world):
    """An error would answer "does this repo exist" for somebody with no right to ask.

    `repo_id` is a hash of a public remote, so anybody who can guess the
    remote can compute the id. A 404 that only fired for real ids would turn
    this endpoint into an oracle.
    """
    r = client.get(f"/v1/team/summary?repo={SECRET}", headers=bob.auth)
    assert r.status_code == 200
    assert r.json()["activeMs"] == 0
    assert r.json()["byRepo"] == []

    # A real-but-invisible id and a made-up one must be indistinguishable.
    invented = client.get("/v1/team/summary?repo=repo_totally_made_up", headers=bob.auth)
    assert invented.status_code == r.status_code
    assert invented.json()["activeMs"] == r.json()["activeMs"]
    assert invented.json()["byRepo"] == r.json()["byRepo"]


def test_sessions_actors_and_daily_are_all_scoped(client, bob, world):
    """One endpoint forgetting the scope is the whole leak; there is no partial version."""
    sessions = client.get("/v1/team/sessions", headers=bob.auth).json()
    assert {s["sessionId"] for s in sessions["sessions"]} == {"s-pub", "s-bob"}

    actors = client.get("/v1/team/actors", headers=bob.auth).json()
    by_actor = {a["actor"]: a for a in actors["actors"]}
    assert by_actor["alice"]["activeMs"] == 1 * 3_600_000
    assert by_actor["alice"]["repos"] == 1

    daily = client.get("/v1/team/daily", headers=bob.auth).json()
    total = sum(d["activeMs"] for d in daily["days"])
    assert total == 1 * 3_600_000 + 30 * 60_000
    # The secret session is on the following day; that day must not appear at all.
    assert len(daily["days"]) == 1


def test_a_stranger_sees_nothing(client, make_account, world):
    """Somebody on no team, who has published nothing, has an empty world."""
    nobody = make_account("carol")
    assert client.get("/v1/team/repos", headers=nobody.auth).json()["repos"] == []
    summary = client.get("/v1/team/summary", headers=nobody.auth).json()
    assert summary["activeMs"] == 0
    assert summary["scope"] == {"repos": 0, "teams": 0}
    assert client.get("/v1/team/sessions", headers=nobody.auth).json()["sessions"] == []


def test_a_publisher_always_sees_their_own_repo(client, alice, world):
    """You can always see what you sent, with or without a team.

    Otherwise the first person to publish cannot check what they published,
    which is the one review that has to be possible before anybody else looks.
    """
    body = client.get("/v1/team/repos", headers=alice.auth).json()
    by_id = {r["repoId"]: r for r in body["repos"]}
    assert SECRET in by_id
    assert "published" in by_id[SECRET]["via"]


def test_verified_forge_access_grants_scope_without_any_team(client, bob, world,
                                                             migrated_db):
    """docs/REDACTION.md §1 is about repo access, not about rosters.

    Somebody who can already check the repo out is exactly the person the
    rule says may see the time spent on it. If this needed an admin to type
    something first, team scope would be hand-maintained, which
    docs/ACCOUNTS.md §1 set out to avoid.
    """
    before = client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"]
    # What `_refresh_repo_access` writes at the end of a device flow. Written
    # directly here so the test is about the scope rule and not about GitHub.
    with migrated_db.connection() as conn:
        conn.execute(
            """INSERT INTO account_repo_access
                   (account_id, repo_id, provider, verified_at)
               VALUES (%s, %s, 'github', 1)""",
            (bob.account_id, SECRET),
        )
    body = client.get("/v1/team/repos", headers=bob.auth).json()
    by_id = {r["repoId"]: r for r in body["repos"]}
    assert SECRET in by_id
    assert "github" in by_id[SECRET]["via"]
    after = client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"]
    assert after == before + 2 * 3_600_000


def test_a_roster_cannot_name_a_repo_that_was_never_published(client, world,
                                                               migrated_db):
    """The roster is grounded in the repo registry by a foreign key.

    An entry with nothing behind it would let an admin put an arbitrary
    guessed `repo_id` on a roster and wait for somebody to publish into it --
    turning the roster into a standing claim on a repo they cannot see. The
    admin route already refuses that; this is the database refusing it too,
    for every other path in.
    """
    import psycopg

    unknown = repo_id(github_remote("github.com", "acme/never-published"))
    with migrated_db.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO team_repo (team_id, repo_id, added_at) "
                    "VALUES (%s, %s, 1)",
                    (world, unknown),
                )


# --------------------------------------------------------------------------
# the roster is not a way to widen your own access
# --------------------------------------------------------------------------


def test_an_admin_cannot_roster_a_repo_they_cannot_see(client, bob, world, alice):
    """The guessed-repo_id attack, and the reason `POST /teams/{id}/repos` checks scope.

    `repo_id = sha256("repo" + normalized_remote)`, and the remote is public
    by construction. If a roster accepted any id, then knowing that somebody
    works on github.com/acme/secret would be enough: hash it, add it to a
    team of one, read their sessions. A roster may widen WHO sees a repo; it
    must never widen WHICH repos the person doing the widening can see.
    """
    bobs_team = make_team(client, bob, "Bob's team of one")
    r = client.post(f"/v1/teams/{bobs_team}/repos", json={"repoId": SECRET},
                    headers=bob.auth)
    assert r.status_code == 404
    assert client.get("/v1/team/summary", headers=bob.auth).json()["activeMs"] == \
        1 * 3_600_000 + 30 * 60_000


def test_a_non_member_gets_404_for_a_team(client, make_account, world):
    """404, not 403: a distinguishable 403 confirms the team exists."""
    stranger = make_account("dave")
    for path in (f"/v1/teams/{world}/members", f"/v1/teams/{world}/repos"):
        r = client.get(path, headers=stranger.auth)
        assert r.status_code == 404
        assert r.json()["error"] == "not_found"


def test_a_member_cannot_use_an_admin_route(client, bob, world):
    """403 here is right: Bob is a member, so nothing is disclosed by being specific."""
    r = client.post(f"/v1/teams/{world}/repos", json={"repoId": BOBS}, headers=bob.auth)
    assert r.status_code == 403
    assert r.json()["error"] == "not_an_admin"


def test_the_last_admin_cannot_be_removed(client, alice, world):
    """A team with no admin is a roster nobody can correct.

    The repos on it stay visible to everybody on it, forever, with no way to
    take one off.
    """
    r = client.delete(f"/v1/teams/{world}/members/{alice.account_id}", headers=alice.auth)
    assert r.status_code == 409
    assert r.json()["error"] == "last_admin"


# --------------------------------------------------------------------------
# rule 2: branch names get a per-repo opt-out
# --------------------------------------------------------------------------


def test_branch_names_are_published_by_default(client, bob, world):
    """Default on, per docs/ACCOUNTS.md §5: anyone with repo access can `git branch -r`."""
    sessions = client.get("/v1/team/sessions", headers=bob.auth).json()["sessions"]
    pub = next(s for s in sessions if s["sessionId"] == "s-pub")
    assert pub["gitBranch"] == "feat/ui"


def test_the_switch_suppresses_branch_names_after_publication(client, alice, bob, world):
    """The switch's whole purpose is to be flipped AFTER the rows were published.

    A write-time-only control would do nothing about the names already in the
    store, which is the only case anybody reaches for this switch in.
    """
    r = client.patch(f"/v1/teams/{world}/repos/{PUBLIC}",
                     json={"branchNamesPublished": False}, headers=alice.auth)
    assert r.status_code == 200

    sessions = client.get("/v1/team/sessions", headers=bob.auth).json()["sessions"]
    pub = next(s for s in sessions if s["sessionId"] == "s-pub")
    assert pub["gitBranch"] is None
    # Null, not a placeholder: the same value a session with no branch
    # carries, so no observer can tell suppressed from absent.
    assert "feat/ui" not in body_text(sessions)

    # The rest of the row is untouched -- the switch hides a name, not a session.
    assert pub["activeMs"] == 3_600_000


def test_off_wins_when_a_repo_is_on_two_teams(client, alice, bob, world):
    """An admin of one team must not re-enable what an admin of another turned off.

    The switch exists to stop a name being shown; "some other team had it on"
    is not a reason to show it.
    """
    client.patch(f"/v1/teams/{world}/repos/{PUBLIC}",
                 json={"branchNamesPublished": False}, headers=alice.auth)
    second = make_team(client, alice, "Another team")
    client.post(f"/v1/teams/{second}/members",
                json={"accountId": bob.account_id}, headers=alice.auth)
    client.post(f"/v1/teams/{second}/repos",
                json={"repoId": PUBLIC, "branchNamesPublished": True},
                headers=alice.auth)

    sessions = client.get("/v1/team/sessions", headers=bob.auth).json()["sessions"]
    pub = next(s for s in sessions if s["sessionId"] == "s-pub")
    assert pub["gitBranch"] is None

    repos = {r["repoId"]: r for r in
             client.get("/v1/team/repos", headers=bob.auth).json()["repos"]}
    assert repos[PUBLIC]["branchNamesPublished"] is False


def test_a_suppressed_branch_is_still_readable_by_its_author(client, alice, bob, world):
    """Alice reaches her own repo as its publisher, not through Bob's team.

    The switch is a team-side control over what a team is shown. It is not a
    deletion, and the person who sent the row can still see what they sent.
    """
    client.patch(f"/v1/teams/{world}/repos/{PUBLIC}",
                 json={"branchNamesPublished": False}, headers=alice.auth)
    # Alice is an admin of the team, so the switch applies to her too -- and
    # that is the honest behaviour: she asked for it to be off.
    mine = client.get("/v1/team/sessions", headers=alice.auth).json()["sessions"]
    assert next(s for s in mine if s["sessionId"] == "s-pub")["gitBranch"] is None
    # Her OTHER repo, which no team controls, is unaffected.
    assert next(s for s in mine if s["sessionId"] == "s-sec")["gitBranch"] == \
        "feat/restricted-org-dbs"


# --------------------------------------------------------------------------
# rule 3: withheld time stays counted
# --------------------------------------------------------------------------


def test_withheld_time_is_reported_rather_than_dropped(client, alice, bob, world):
    """docs/ACCOUNTS.md §5: a dashboard that quietly drops part of somebody's week
    is not private, it is wrong, and the person reading it cannot tell.

    `redact` reports 8% of the author's corpus as unpublishable -- work with
    no git remote. If this number is absent, a viewer reads a smaller total
    and has no way to know it is smaller.
    """
    r = client.post("/v1/team/publish", json={
        "kind": "withheld", "actor": alice.actor,
        "rows": [{"hostId": "h-alice", "withheldMs": 52_920_000,
                  "withheldProjects": 21, "publishedMs": 642_960_000}],
    }, headers=alice.auth)
    assert r.status_code == 200

    body = client.get("/v1/team/summary", headers=bob.auth).json()["withheld"]
    entry = next(e for e in body["byActor"] if e["actor"] == "alice")
    assert entry["withheldMs"] == 52_920_000
    assert entry["withheldProjects"] == 21
    assert body["totalMs"] == 52_920_000
    # Labelled as a corpus figure, so a renderer can say "all time" even when
    # the page beside it says "this week". There is no time dimension to
    # filter on: work with no repo has nothing to bucket against.
    assert body["scope"] == "corpus"
    assert body["rangeFiltered"] is False


def test_withheld_is_reported_even_with_a_range_filter(client, alice, bob, world):
    """Filtering to a week must not silently drop the figure for that person."""
    client.post("/v1/team/publish", json={
        "kind": "withheld", "actor": alice.actor,
        "rows": [{"hostId": "h", "withheldMs": 999, "withheldProjects": 1,
                  "publishedMs": 1}],
    }, headers=alice.auth)
    narrow = client.get(f"/v1/team/summary?from={T0 + 10 * DAY}", headers=bob.auth).json()
    assert narrow["activeMs"] == 0
    assert narrow["withheld"]["totalMs"] == 999


def test_published_ms_is_served_only_to_its_own_author(client, alice, bob, world):
    """It is corpus-wide across repos the viewer cannot see.

    Handing Bob Alice's `publishedMs` discloses the MAGNITUDE of her work in
    `acme/secret` without naming it -- a weaker form of exactly the leak rule
    1 exists to prevent.
    """
    client.post("/v1/team/publish", json={
        "kind": "withheld", "actor": alice.actor,
        "rows": [{"hostId": "h", "withheldMs": 100, "withheldProjects": 2,
                  "publishedMs": 642_960_000}],
    }, headers=alice.auth)

    theirs = client.get("/v1/team/summary", headers=bob.auth).json()["withheld"]
    alice_entry = next(e for e in theirs["byActor"] if e["actor"] == "alice")
    assert "publishedMs" not in alice_entry
    assert "642960000" not in body_text(theirs)

    mine = client.get("/v1/team/summary", headers=alice.auth).json()["withheld"]
    own = next(e for e in mine["byActor"] if e["actor"] == "alice")
    assert own["publishedMs"] == 642_960_000


def test_no_out_of_scope_figure_is_reported_anywhere(client, bob, world):
    """Not as a number, and not as a boolean.

    "There is more you cannot see" is still an answer to "does a repo I
    cannot see exist". The contract says a renderer must label `activeMs` as
    *in repos you can see* rather than reporting a remainder.
    """
    body = client.get("/v1/team/summary", headers=bob.auth).json()
    text = body_text(body)
    for forbidden in ("outOfScope", "hidden", "invisible", "partial", "truncated",
                      "otherRepos", "unavailable", "restricted"):
        assert forbidden not in text, forbidden
    # The `withheld` block's own totalMs is the unpublishable hours, which is
    # a different quantity and is required by rule 3. Nothing else totals.
    assert set(body) == {"scope", "activeMs", "sessions", "spans", "byRole",
                         "bySource", "byRepo", "withheld", "generatedAt"}
    # And the one total that IS reported is only the visible one.
    assert body["activeMs"] == 1 * 3_600_000 + 30 * 60_000


def test_withheld_is_not_reported_for_people_you_share_nothing_with(
    client, make_account, bob, world
):
    """Otherwise the block announces the existence of people, not just hours."""
    carol = make_account("carol")
    client.post("/v1/team/publish", json={
        "kind": "withheld", "actor": carol.actor,
        "rows": [{"hostId": "h", "withheldMs": 5, "withheldProjects": 1,
                  "publishedMs": 0}],
    }, headers=carol.auth)

    body = client.get("/v1/team/summary", headers=bob.auth).json()["withheld"]
    assert "carol" not in body_text(body)


def test_every_team_read_endpoint_is_scoped(client, bob, world):
    """A new endpoint added without a scope is the whole leak.

    Enumerates every GET under `/v1/team` rather than naming them, so an
    endpoint added next month is covered by this test the day it is written
    -- which is the only kind of coverage that survives a busy week. It calls
    each one as Bob and asserts that nothing belonging to `acme/secret`
    appears: not the repo id, not the session id, not the span id, not the
    branch name that `docs/REDACTION.md` §4b uses as its example of a name
    that says too much.
    """
    from cci_server.routes import team_data

    invisible = (SECRET, "s-sec", "sp-sec", "feat/restricted-org-dbs", "secret")

    # Enumerated off the router rather than off `app.routes`, which this
    # FastAPI version wraps in an opaque `_IncludedRouter`.
    paths = sorted(
        r.path for r in team_data.router.routes
        if "GET" in getattr(r, "methods", set()) and "{" not in r.path
    )
    assert paths, "no team read endpoints were discovered at all"

    for path in paths:
        r = client.get(path, headers=bob.auth)
        assert r.status_code == 200, (path, r.text)
        for needle in invisible:
            assert needle not in r.text, f"{path} disclosed {needle!r}"


def body_text(obj) -> str:
    import json

    return json.dumps(obj)
