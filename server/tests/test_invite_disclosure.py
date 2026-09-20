"""Joining a team must not, by itself, widen what anybody can see.

`test_scope_bypass_regression.py` records a bypass where publishing a guessed
`repo_id` granted read access to everyone's rows on it.
`test_roster_escalation.py` records the same hole reappearing one step later,
through a roster. Both were closed by the same idea, which is the one thing in
this system that must not be eroded:

    Nothing a caller ASSERTS about themselves grants them anything. A roster
    delegates the ADDER'S verified access and no more.

An invite is a new way for a person to arrive inside a team, so it is a new
candidate for the same laundering step, and this file is the argument that it
is not one. The claim being tested has two directions and both need saying,
because they fail differently:

  FORWARD  -- what can a new member see the moment they join? Exactly what
              the roster already delegated to that team, which is bounded by
              `team_repo.added_by`. Joining adds a person to a set; it does
              not add a repo to a roster, and it does not make the adder
              retroactively more trusted.

  REVERSE  -- what can the existing members now see of the joiner? Nothing
              they could not see before. This is the direction that is easy
              to get wrong, because "we are on a team together" is such a
              natural thing to key a disclosure off -- and `scope.resolve`
              deliberately keys off nothing of the kind. There is no term in
              it for co-membership. The tests below are what stop one being
              added by somebody who thinks it is obviously fine.

The structural reason both hold: `scope.resolve` reads `team_member` ONLY to
find which rosters apply, and every row it then grants is attributed to
`added_by` or to `account_repo_access`. Membership is a join key, never a
grant. If a future change makes it a grant, several of these fail.
"""

from __future__ import annotations

from conftest import join_team

from cci_server.repoid import repo_id

SECRET = "https://github.com/acme/payments"
OPEN = "https://github.com/acme/toolkit"


def make_team(client, who, name="Platform") -> str:
    r = client.post("/v1/teams", json={"name": name}, headers=who.auth)
    assert r.status_code == 201, r.text
    return r.json()["teamId"]


def publish_repo(client, who, remote: str) -> str:
    rid = repo_id(remote)
    owner, repo = remote.rsplit("/", 2)[-2:]
    r = client.post("/v1/team/publish", headers=who.auth, json={
        "kind": "repos", "rows": [{
            "repoId": rid, "remoteUrl": remote, "forge": "github",
            "owner": owner, "repo": repo, "webUrl": remote, "name": repo}]})
    assert r.status_code == 200, r.text
    return rid


def publish_session(client, who, rid: str, sid: str, branch="main", ms=60_000) -> None:
    r = client.post("/v1/team/publish", headers=who.auth, json={
        "kind": "sessions", "rows": [{
            "sessionId": sid, "repoId": rid, "actor": who.actor,
            "source": "claude_code", "gitBranch": branch,
            "startedAt": 1_700_000_000_000, "endedAt": 1_700_000_000_000 + ms,
            "activeMs": ms, "eventCount": 10}]})
    assert r.status_code == 200, r.text
    r = client.post("/v1/team/publish", headers=who.auth, json={
        "kind": "spans", "rows": [{
            "spanId": f"sp-{sid}", "sessionId": sid, "threadRole": "human",
            "startedAt": 1_700_000_000_000, "endedAt": 1_700_000_000_000 + ms,
            "eventCount": 10}]})
    assert r.status_code == 200, r.text


def verify_forge_access(migrated_db, account, rid: str) -> None:
    """What `cci login` does when GitHub says this person can reach the repo.

    The only source of full access in this system that does not come from
    another account (`scope.VIA_FORGE`). Written directly here for the same
    reason `make_account` bypasses the device flow: sign-in is tested on its
    own and this test is about what the access is WORTH.
    """
    with migrated_db.connection() as conn:
        conn.execute(
            """INSERT INTO account_repo_access
                   (account_id, repo_id, provider, verified_at)
               VALUES (%s, %s, 'github', 1)
               ON CONFLICT DO NOTHING""",
            (account.account_id, rid),
        )


def visible(client, who) -> dict:
    """Everything this account can see, as one comparable blob.

    Every scope-bearing read endpoint at once, because the question these
    tests ask is "did ANYTHING widen" and checking three of the five is how a
    fourth quietly starts disclosing. `scope.py` makes the same argument for
    enumerating the team router's GET routes rather than naming them.
    """
    return {
        "repos": sorted(r["repoId"] for r in
                        client.get("/v1/team/repos", headers=who.auth).json()["repos"]),
        "sessions": sorted(
            (s["sessionId"], s["actor"], s["gitBranch"]) for s in
            client.get("/v1/team/sessions", headers=who.auth).json()["sessions"]),
        "actors": sorted(a["actor"] for a in
                         client.get("/v1/team/actors", headers=who.auth).json()["actors"]),
        "activeMs": client.get("/v1/team/summary", headers=who.auth).json()["activeMs"],
        "daily": client.get("/v1/team/daily", headers=who.auth).json()["days"],
    }


# --------------------------------------------------------------------------
# FORWARD: what a new member can see at the moment they join
# --------------------------------------------------------------------------


def test_joining_an_empty_team_reveals_absolutely_nothing(client, alice, bob):
    """The baseline, and the one that would catch the crudest mistake.

    A team with no repos on its roster has nothing to share, so redeeming a
    code for it must leave the joiner's visible world byte-for-byte
    identical. If membership were itself a grant -- if `scope.resolve` had
    any term reading "rows belonging to people on my teams" -- this is where
    it would show, because Alice has published data and Bob has not.
    """
    rid = publish_repo(client, alice, SECRET)
    publish_session(client, alice, rid, "sess-alice", branch="feat/unreleased")

    before = visible(client, bob)
    assert before["repos"] == [] and before["sessions"] == []

    team = make_team(client, alice, "Alice's team")
    join_team(client, alice, team, bob)

    assert visible(client, bob) == before, "joining a team disclosed something"
    # And specifically, none of Alice's work by any name.
    for path in ("/v1/team/sessions", "/v1/team/actors", "/v1/team/repos"):
        body = client.get(path, headers=bob.auth).text
        assert "sess-alice" not in body
        assert "feat/unreleased" not in body
        assert alice.actor not in body


def test_an_invite_cannot_launder_a_self_asserted_repo(
    client, alice, bob, make_account
):
    """The four-call escalation, with an invite bolted on as a fifth step.

    `test_roster_escalation.py` ends with Mallory reading only her own row.
    The obvious next thing to try is to make somebody ELSE do the reading:

        publish a guessed repo id
        publish one row of your own under it
        create a team of one
        roster the repo you now "have access to"
        MINT A CODE AND HAVE A COLLEAGUE JOIN

    If an invite were a laundering step, the joiner -- who never guessed
    anything and looks entirely legitimate -- would come out the other side
    holding Alice's sessions. They must not, and the reason is that the
    roster granted `added_by`'s access at the moment it was written and the
    arrival of a new member does not revisit that.

    The colleague is Bob, and he is not an attacker. That is the point: the
    laundering attack works by making the disclosure land on somebody
    innocent, so a test where the joiner is obviously villainous would miss
    it.
    """
    rid = publish_repo(client, alice, SECRET)
    publish_session(client, alice, rid, "sess-alice", branch="feat/unreleased")

    mallory = make_account("mallory")
    publish_repo(client, mallory, SECRET)          # a guessed id, asserted
    publish_session(client, mallory, rid, "sess-mallory")

    team = make_team(client, mallory, "Totally Normal Team")
    assert client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                       headers=mallory.auth).status_code == 201

    join_team(client, mallory, team, bob)

    body = client.get("/v1/team/sessions", headers=bob.auth).text
    assert "sess-alice" not in body, "an invite laundered a guessed repo id"
    assert "feat/unreleased" not in body, "LEAK: Alice's branch name"
    assert alice.actor not in client.get("/v1/team/actors", headers=bob.auth).text

    # Bob got exactly what Mallory was entitled to share: Mallory's own row.
    assert "sess-mallory" in body
    seen = visible(client, bob)
    assert seen["actors"] == ["mallory"]
    assert seen["activeMs"] == 60_000, "Alice's time reached Bob's aggregate"


def test_a_new_member_gets_the_adders_access_and_not_a_byte_more(
    client, alice, bob, make_account, migrated_db
):
    """Delegation works through the invite path, and it is still bounded.

    Two repos, and the difference between them is the whole rule. Carol
    rostered both; GitHub verified her for one of them and not the other. Bob
    joins by code and must land on exactly that line -- every row in the repo
    Carol could really reach, and only Carol's own rows in the one she could
    not.

    Without the "only the adder's access" half, this test reads Alice's row
    in `SECRET` too, which is the roster escalation again.
    """
    open_repo = publish_repo(client, alice, OPEN)
    secret = publish_repo(client, alice, SECRET)
    publish_session(client, alice, open_repo, "sess-alice-open")
    publish_session(client, alice, secret, "sess-alice-secret")

    carol = make_account("carol")
    publish_repo(client, carol, OPEN)
    publish_repo(client, carol, SECRET)
    publish_session(client, carol, open_repo, "sess-carol-open")
    publish_session(client, carol, secret, "sess-carol-secret")
    # GitHub says Carol can reach the toolkit. It says nothing about payments.
    verify_forge_access(migrated_db, carol, open_repo)

    team = make_team(client, carol, "Carol's")
    for rid in (open_repo, secret):
        assert client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                           headers=carol.auth).status_code == 201

    join_team(client, carol, team, bob)

    seen = sorted(s[0] for s in visible(client, bob)["sessions"])
    assert seen == ["sess-alice-open", "sess-carol-open", "sess-carol-secret"], seen
    assert "sess-alice-secret" not in seen, (
        "the invite delegated access the rostering admin did not have"
    )


def test_a_new_member_cannot_roster_anything_themselves(client, alice, bob):
    """Joining does not hand over the one action that widens a team's reach.

    A code minted for `member` lands the redeemer as a member, and rostering
    is an admin action. This matters because `add_repo` is the only call in
    the system that changes what OTHER people can see, so "what role does a
    code confer" is a security question rather than a convenience one.
    """
    rid = publish_repo(client, bob, OPEN)
    publish_session(client, bob, rid, "sess-bob")

    team = make_team(client, alice, "Alice's")
    joined = join_team(client, alice, team, bob)
    assert joined["role"] == "member"

    refused = client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                          headers=bob.auth)
    assert refused.status_code == 403
    assert refused.json()["error"] == "not_an_admin"
    assert client.get(f"/v1/teams/{team}/repos",
                      headers=alice.auth).json()["repos"] == []


# --------------------------------------------------------------------------
# REVERSE: what the existing members can see of the person who joined
# --------------------------------------------------------------------------


def test_joining_exposes_none_of_the_joiners_own_rows_to_the_team(
    client, alice, bob, make_account
):
    """The direction that is easy to get wrong, and the one nobody looks at.

    Bob has published work in a repo of his own. He joins Alice's team. Alice
    must see nothing of it -- because a roster is what delegates, Bob's repo
    is not on hers, and nothing about standing next to somebody grants sight
    of their work.

    The tempting bug here is a scope term reading "rows published by people
    on my teams", which sounds collegial and would be a disclosure: it would
    make every person who joins a team retroactively publish their whole
    history to it, including repos the team has never heard of.
    """
    bobs_repo = publish_repo(client, bob, "https://github.com/bob/sideproject")
    publish_session(client, bob, bobs_repo, "sess-bob-private", branch="spike/quitting")

    alices_repo = publish_repo(client, alice, OPEN)
    publish_session(client, alice, alices_repo, "sess-alice")
    team = make_team(client, alice, "Alice's")
    assert client.post(f"/v1/teams/{team}/repos", json={"repoId": alices_repo},
                       headers=alice.auth).status_code == 201

    before = visible(client, alice)
    join_team(client, alice, team, bob)
    after = visible(client, alice)

    assert after == before, "the new member's arrival widened the admin's view"
    for path in ("/v1/team/sessions", "/v1/team/repos", "/v1/team/actors"):
        body = client.get(path, headers=alice.auth).text
        assert "sess-bob-private" not in body, "LEAK: the joiner's own session"
        assert "spike/quitting" not in body, "LEAK: the joiner's branch name"
        assert bobs_repo not in body, "LEAK: the existence of the joiner's repo"


def test_the_teams_view_of_a_joiner_comes_from_the_roster_not_the_membership(
    client, alice, bob, migrated_db
):
    """And here is the case where the team DOES see the joiner's rows.

    Stated as its own test so the one above cannot be mistaken for "a team
    never sees a new member's work". Alice is forge-verified for the repo and
    rostered it, so her team reads every row in it -- including rows Bob
    published there. What made them visible is the verified roster, which
    existed before Bob arrived and would have covered his rows whoever he
    was. Joining did not widen anything; it added a reader to a set that was
    already defined.

    The distinction is not academic. If this were keyed on membership
    instead, Bob joining would also expose his rows in repos NOT on the
    roster -- which is exactly what the previous test forbids.
    """
    rid = publish_repo(client, alice, OPEN)
    publish_session(client, alice, rid, "sess-alice")
    verify_forge_access(migrated_db, alice, rid)
    team = make_team(client, alice, "Alice's")
    assert client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                       headers=alice.auth).status_code == 201

    # Bob publishes into the SAME repo, before joining. Alice can already see
    # it, because a verified roster covers the repo rather than its members.
    publish_repo(client, bob, OPEN)
    publish_session(client, bob, rid, "sess-bob-shared")
    before = visible(client, alice)
    assert "sess-bob-shared" in [s[0] for s in before["sessions"]]

    join_team(client, alice, team, bob)
    assert visible(client, alice) == before, "joining changed what the team sees"


def test_redeeming_an_admin_code_rosters_nothing_by_itself(
    client, alice, bob, migrated_db
):
    """An admin who joins brings their access with them only if they SPEND it.

    Redeeming an `admin` code makes Bob able to roster repos he can see,
    which would delegate his verified access to Alice's team. That is a real
    widening and it is fine -- it is a deliberate act by the person whose
    access it is. What must not happen is it happening automatically, so that
    minting an admin code for somebody becomes a way to harvest their scope.

    The test is that Alice's view is unchanged by Bob's arrival, and changes
    only when Bob himself rosters something.
    """
    rid = publish_repo(client, bob, SECRET)
    publish_session(client, bob, rid, "sess-bob")
    verify_forge_access(migrated_db, bob, rid)
    # Somebody else's rows in the same repo, so "Bob's verified access" is
    # worth strictly more than "Bob's own rows" and the difference is visible.
    publish_repo(client, alice, SECRET)
    publish_session(client, alice, rid, "sess-alice")

    team = make_team(client, alice, "Alice's")
    before = visible(client, alice)
    joined = join_team(client, alice, team, bob, role="admin")
    assert joined["role"] == "admin"

    assert visible(client, alice) == before, (
        "minting an admin code harvested the joiner's scope"
    )
    assert "sess-bob" not in client.get("/v1/team/sessions", headers=alice.auth).text

    # Bob, deliberately, shares it. Now Alice sees it -- via the roster,
    # attributed to Bob, and only because he chose to.
    assert client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                       headers=bob.auth).status_code == 201
    now = [s[0] for s in visible(client, alice)["sessions"]]
    assert "sess-bob" in now and "sess-alice" in now


# --------------------------------------------------------------------------
# the branch-name switch, which is the one thing joining really does change
# --------------------------------------------------------------------------


def test_joining_only_ever_uncovers_branch_names_on_repos_you_already_reach(
    client, alice, bob, make_account, migrated_db
):
    """The one widening joining can cause, and why it is not a boundary crossing.

    `scope._branch_suppressed` has two halves: a caller INSIDE a team is
    governed by their own teams, and a caller in no relevant team gets the
    strictest switch anybody set. So joining a team whose roster has the
    switch ON can lift a suppression that another team's admin had imposed.
    That is a real change in what is rendered, and it is worth being explicit
    about rather than discovering later.

    It is not a disclosure, for a reason specific to this column. Bob is
    forge-verified for the repo, which means GitHub says he can clone it and
    run `git branch -r`. `docs/SERVER_API.md` §4.5 publishes branch names
    under exactly that rule -- anyone with repo access can read them anyway
    -- and the switch is a per-team courtesy over a field that is already
    within his reach, not a boundary over one that is not.

    What this test pins is the LIMIT: the uncovering happens only for a repo
    Bob could already see every row of. It can never reveal a branch name in
    a repo joining did not otherwise give him.
    """
    rid = publish_repo(client, alice, OPEN)
    publish_session(client, alice, rid, "sess-alice", branch="feat/secret-client")
    verify_forge_access(migrated_db, bob, rid)

    # A team Bob is NOT on turns branch names off. Under half 2 that binds
    # him, because no team of his speaks for him.
    carol = make_account("carol")
    verify_forge_access(migrated_db, carol, rid)
    strict = make_team(client, carol, "Strict")
    client.post(f"/v1/teams/{strict}/repos",
                json={"repoId": rid, "branchNamesPublished": False},
                headers=carol.auth)

    before = visible(client, bob)
    assert rid in before["repos"], "forge access already reaches the repo"
    assert [s[2] for s in before["sessions"]] == [None], "the switch is off for Bob"

    relaxed = make_team(client, alice, "Relaxed")
    client.post(f"/v1/teams/{relaxed}/repos",
                json={"repoId": rid, "branchNamesPublished": True}, headers=alice.auth)
    join_team(client, alice, relaxed, bob)

    after = visible(client, bob)
    # The branch name is now shown -- documented above, and a column he could
    # have read from git regardless.
    assert [s[2] for s in after["sessions"]] == ["feat/secret-client"]
    # And that is ALL that changed. No new repo, no new row, no new person,
    # no change to the totals.
    assert after["repos"] == before["repos"]
    assert after["activeMs"] == before["activeMs"]
    assert after["actors"] == before["actors"]
    assert [s[0] for s in after["sessions"]] == [s[0] for s in before["sessions"]]


def test_joining_a_team_with_the_switch_off_never_uncovers_anything(
    client, alice, bob, migrated_db
):
    """Off wins, and joining cannot be used to argue otherwise.

    The mirror of the test above: if the team being joined has the switch
    off, the joiner sees fewer branch names, never more. The direction to be
    wrong in is under-disclosure, and this is the test that keeps it that
    way.
    """
    rid = publish_repo(client, alice, OPEN)
    publish_session(client, alice, rid, "sess-alice", branch="feat/secret-client")
    verify_forge_access(migrated_db, bob, rid)
    assert [s[2] for s in visible(client, bob)["sessions"]] == ["feat/secret-client"]

    team = make_team(client, alice, "Careful")
    client.post(f"/v1/teams/{team}/repos",
                json={"repoId": rid, "branchNamesPublished": False}, headers=alice.auth)
    join_team(client, alice, team, bob)

    assert [s[2] for s in visible(client, bob)["sessions"]] == [None]


# --------------------------------------------------------------------------
# leaving
# --------------------------------------------------------------------------


def test_leaving_a_team_takes_the_delegated_view_away_again(
    client, alice, bob, migrated_db
):
    """Membership is checked at read time, so leaving is effective immediately.

    Worth a test because the alternative -- a cached or materialised scope --
    is the optimisation `docs/ACCOUNTS.md` §5 rule 1 already forbids for
    aggregates, and it would fail here in the disclosure direction: somebody
    who left a team continuing to read its roster.
    """
    rid = publish_repo(client, alice, OPEN)
    publish_session(client, alice, rid, "sess-alice")
    verify_forge_access(migrated_db, alice, rid)
    team = make_team(client, alice, "Alice's")
    client.post(f"/v1/teams/{team}/repos", json={"repoId": rid}, headers=alice.auth)

    join_team(client, alice, team, bob)
    assert [s[0] for s in visible(client, bob)["sessions"]] == ["sess-alice"]

    assert client.delete(f"/v1/teams/{team}/members/{bob.account_id}",
                         headers=bob.auth).status_code == 204
    gone = visible(client, bob)
    assert gone["sessions"] == [] and gone["repos"] == []
