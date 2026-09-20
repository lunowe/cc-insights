"""The scope bypass, written as the attack it actually is.

`teams.add_repo` locks one door: an admin may only roster a repo already in
their own scope, because otherwise *knowing* that somebody works on
github.com/acme/secret is enough -- hash it, add it to a team of one, read
their sessions. That reasoning is in `routes/teams.py`'s docstring and the
guard implementing it is correct.

`POST /v1/team/publish` with `kind=repos` opened onto the same room and had
no lock at all. It wrote `repo_publisher(repo_id, caller)` for whatever ids
the body named, and `scope.resolve` treats a `repo_publisher` row as term 2
of the scope union -- independently sufficient evidence of repo access. Two
HTTP calls and a guessed remote read everything.

`test_a_stranger_sees_nothing` did not catch it, and the reason is worth
keeping: its docstring says "somebody on no team, WHO HAS PUBLISHED
NOTHING". The parenthetical excludes precisely the action that grants the
access.

`repo_id` is not a secret and cannot be made one -- REDACTION.md §0 is the
whole argument for keying on the remote, and it works only because the
remote is public on the far side of the boundary. So the id being guessable
is by design; treating a self-asserted one as proof of access is the bug.
"""

from __future__ import annotations

from conftest import join_team, session_row  # noqa: F401  (session_row: parity with siblings)

from cci_server.repoid import repo_id

SECRET_REMOTE = "https://github.com/acme/payments"


def _publish_repo(client, who, remote: str) -> str:
    rid = repo_id(remote)
    resp = client.post(
        "/v1/team/publish",
        headers=who.auth,
        json={"kind": "repos", "rows": [{
            "repoId": rid, "remoteUrl": remote, "forge": "github",
            "owner": "acme", "repo": "payments",
            "webUrl": remote, "name": "payments",
        }]},
    )
    assert resp.status_code == 200, resp.text
    return rid


def _publish_session(client, who, rid: str, session_id: str, branch: str) -> None:
    resp = client.post(
        "/v1/team/publish",
        headers=who.auth,
        json={"kind": "sessions", "rows": [{
            "sessionId": session_id, "repoId": rid, "actor": who.actor,
            "source": "claude_code", "gitBranch": branch,
            "startedAt": 1_700_000_000_000, "endedAt": 1_700_000_060_000,
            "activeMs": 60_000, "eventCount": 10,
        }]},
    )
    assert resp.status_code == 200, resp.text


def test_a_self_asserted_repo_does_not_grant_read_access(client, alice, bob):
    """Bob claims Alice's private repo by id and must still see nothing.

    The full attack: Bob guesses the remote (a company name and a plausible
    repo name), computes the id -- `repoid.py` is four lines and the
    algorithm is in the published contract -- and POSTs it as a repo he
    publishes. Before the fix that single call put him in scope, and
    `GET /v1/team/sessions?repo=<id>` then returned Alice's actor, her
    timings and her branch names.
    """
    rid = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, rid, "sess-alice-1", "feat/unreleased-thing")

    # Bob asserts the same repo. Whether the write is refused or merely
    # ineffective is an implementation choice; what must not happen is that
    # it becomes evidence of access.
    client.post(
        "/v1/team/publish",
        headers=bob.auth,
        json={"kind": "repos", "rows": [{
            "repoId": rid, "remoteUrl": SECRET_REMOTE, "forge": "github",
            "owner": "acme", "repo": "payments",
            "webUrl": SECRET_REMOTE, "name": "payments",
        }]},
    )

    sessions = client.get(f"/v1/team/sessions?repo={rid}", headers=bob.auth)
    body = sessions.text
    assert "sess-alice-1" not in body, "Bob read a session on a repo he cannot see"
    assert "feat/unreleased-thing" not in body, "Bob read a branch name"
    assert alice.actor not in body, "Bob learned who works on the repo"

    summary = client.get("/v1/team/summary", headers=bob.auth)
    assert "60000" not in summary.text, "Alice's time reached Bob's aggregate"

    repos = client.get("/v1/team/repos", headers=bob.auth)
    assert rid not in repos.text, "the repo itself is disclosed to Bob"


def test_publishing_a_repo_does_not_let_you_rewrite_it(client, alice, bob):
    """The upsert let a stranger silently rewrite name, remote and web URL.

    Sending the correct values keeps the intrusion invisible; sending wrong
    ones misattributes every real member's view of the repo. Either way the
    row is not the publisher's to edit.
    """
    rid = _publish_repo(client, alice, SECRET_REMOTE)

    client.post(
        "/v1/team/publish",
        headers=bob.auth,
        json={"kind": "repos", "rows": [{
            "repoId": rid, "remoteUrl": SECRET_REMOTE, "forge": "github",
            "owner": "acme", "repo": "payments",
            "webUrl": "https://evil.example/acme/payments",
            "name": "TOTALLY DIFFERENT",
        }]},
    )

    seen = client.get("/v1/team/repos", headers=alice.auth).text
    assert "TOTALLY DIFFERENT" not in seen
    assert "evil.example" not in seen


def test_the_stranger_test_now_covers_having_published(client, alice, bob):
    """Close the gap the original test's own docstring carved out.

    `test_a_stranger_sees_nothing` says "on no team, who has published
    nothing". This is the same assertion for somebody who HAS published --
    to a repo of their own, which must not widen what else they can see.
    """
    alice_repo = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, alice_repo, "sess-alice-2", "main")

    bob_repo = _publish_repo(client, bob, "https://github.com/bob/sideproject")
    _publish_session(client, bob, bob_repo, "sess-bob-1", "main")

    visible = client.get("/v1/team/repos", headers=bob.auth).text
    assert bob_repo in visible, "Bob cannot see his own repo"
    assert alice_repo not in visible, "publishing one repo revealed another"


def _publish_span(client, who, session_id: str, span_id: str) -> None:
    resp = client.post(
        "/v1/team/publish",
        headers=who.auth,
        json={"kind": "spans", "rows": [{
            "spanId": span_id, "sessionId": session_id, "threadRole": "human",
            "startedAt": 1_700_000_000_000, "endedAt": 1_700_000_060_000,
            "eventCount": 10,
        }]},
    )
    assert resp.status_code == 200, resp.text


def test_publishing_then_rostering_still_reaches_nobody_elses_rows(client, alice, bob):
    """The same attack, one step further along, and this is the step that bites.

    Making publication a row-level grant is not enough on its own. A roster
    is scope-checked against "can the admin see this repo", and a publisher
    CAN now see the repo -- their own rows in it. So the two-call bypass
    becomes a four-call one: publish a guessed id, publish a row under it,
    create a team of one, roster the repo. If a roster conferred full sight
    of the repo, that would read everything, and the scope check in
    `teams.add_repo` could not tell this apart from a legitimate first
    publisher rostering their own repo -- because at that moment the two are
    identical.

    So a roster hands the team what the ADMIN HAD, not what the repo holds.
    Bob shared his own rows with his own team, which is exactly what he was
    entitled to share and no more.
    """
    rid = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, rid, "sess-alice-3", "feat/unreleased-thing")
    _publish_span(client, alice, "sess-alice-3", "sp-alice-3")

    _publish_repo(client, bob, SECRET_REMOTE)
    _publish_session(client, bob, rid, "sess-bob-2", "main")
    _publish_span(client, bob, "sess-bob-2", "sp-bob-2")

    team = client.post("/v1/teams", json={"name": "Team of one"},
                       headers=bob.auth).json()["teamId"]
    rostered = client.post(f"/v1/teams/{team}/repos", json={"repoId": rid},
                           headers=bob.auth)
    assert rostered.status_code == 201, "a publisher may still roster their own repo"

    body = client.get("/v1/team/sessions", headers=bob.auth).text
    assert "sess-bob-2" in body, "Bob cannot see the row he published"
    assert "sess-alice-3" not in body, "rostering a guessed repo read Alice's session"
    assert "feat/unreleased-thing" not in body
    assert alice.actor not in body

    summary = client.get("/v1/team/summary", headers=bob.auth).json()
    assert summary["activeMs"] == 60_000, "Alice's time reached Bob's aggregate"
    actors = client.get("/v1/team/actors", headers=bob.auth).json()["actors"]
    assert [a["actor"] for a in actors] == [bob.actor]


def test_a_teammate_of_the_rostering_admin_gets_the_admins_access_and_no_more(
    client, alice, bob, make_account
):
    """The other half of the same rule: delegation works, and it is bounded.

    A roster has to keep working on an instance with no GitHub app
    configured -- docs/SERVER_API.md §4.6 says so -- so an admin who can see
    only their own rows must still be able to share those. What they must
    not be able to share is somebody else's.
    """
    rid = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, rid, "sess-alice-4", "main")
    _publish_span(client, alice, "sess-alice-4", "sp-alice-4")

    _publish_repo(client, bob, SECRET_REMOTE)
    _publish_session(client, bob, rid, "sess-bob-3", "main")
    _publish_span(client, bob, "sess-bob-3", "sp-bob-3")

    carol = make_account("carol")
    team = client.post("/v1/teams", json={"name": "Bob's"},
                       headers=bob.auth).json()["teamId"]
    # Carol joins by redeeming a code Bob minted, which is now the only way
    # onto a roster. The assertion below is unchanged and has to be: consent
    # decides WHO is on the team, never WHAT the team can see.
    join_team(client, bob, team, carol)
    client.post(f"/v1/teams/{team}/repos", json={"repoId": rid}, headers=bob.auth)

    body = client.get("/v1/team/sessions", headers=carol.auth).text
    assert "sess-bob-3" in body, "the roster shared nothing at all"
    assert "sess-alice-4" not in body, "the roster shared somebody the admin could not see"


def test_publishing_a_session_is_not_an_existence_oracle(client, alice, bob):
    """A 200-vs-409 answered "does anybody here work on acme/skunkworks".

    The order check asked `published_repo` GLOBALLY, so publishing a session
    under a guessed `repo_id` succeeded exactly when somebody else had
    already registered that repo -- and the 409 echoed the ids back to make
    reading the answer easy. docs/REDACTION.md §0's defence is that the
    remote is public on the far side of the boundary; that is true of a
    public repo and simply false of a private one.

    A real-but-unregistered id and an invented one must now be
    indistinguishable, because the answer depends only on what the caller
    themselves has published.
    """
    real = _publish_repo(client, alice, SECRET_REMOTE)
    invented = "0" * 32

    probe = client.post("/v1/team/publish", headers=bob.auth, json={
        "kind": "sessions", "rows": [{
            "sessionId": "sess-probe", "repoId": real, "actor": bob.actor,
            "source": "claude_code", "startedAt": 1, "endedAt": 2,
            "activeMs": 1, "eventCount": 0}]})
    control = client.post("/v1/team/publish", headers=bob.auth, json={
        "kind": "sessions", "rows": [{
            "sessionId": "sess-control", "repoId": invented, "actor": bob.actor,
            "source": "claude_code", "startedAt": 1, "endedAt": 2,
            "activeMs": 1, "eventCount": 0}]})

    assert probe.status_code == control.status_code == 409
    assert probe.json()["error"] == control.json()["error"]
    # The message names the id the caller sent, which they already know, and
    # says the same thing in both cases.
    assert probe.json()["message"].replace(real, "X") == \
        control.json()["message"].replace(invented, "X")


def test_a_verified_admin_re_adding_the_repo_upgrades_the_team(
    client, alice, bob, make_account, migrated_db
):
    """Delegation under-shares by design, and this is how it is corrected.

    Bob rostered the repo with only publisher-level access, so his team saw
    his rows alone. An admin who GitHub says can reach the repo adding it
    again replaces `added_by`, and the team's view becomes the full one --
    which is the same authority `teams.add_repo` already requires, arriving
    through the documented route rather than through a special case.
    """
    rid = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, rid, "sess-alice-5", "main")
    _publish_span(client, alice, "sess-alice-5", "sp-alice-5")

    _publish_repo(client, bob, SECRET_REMOTE)
    _publish_session(client, bob, rid, "sess-bob-4", "main")
    _publish_span(client, bob, "sess-bob-4", "sp-bob-4")

    carol = make_account("carol")
    team = client.post("/v1/teams", json={"name": "Platform"},
                       headers=bob.auth).json()["teamId"]
    for member in (alice, carol):
        join_team(client, bob, team, member, role="admin")
    client.post(f"/v1/teams/{team}/repos", json={"repoId": rid}, headers=bob.auth)

    assert "sess-alice-5" not in client.get("/v1/team/sessions", headers=carol.auth).text

    # Alice signs in and GitHub confirms she can reach the repo.
    with migrated_db.connection() as conn:
        conn.execute(
            """INSERT INTO account_repo_access
                   (account_id, repo_id, provider, verified_at)
               VALUES (%s, %s, 'github', 1)""",
            (alice.account_id, rid),
        )
    client.post(f"/v1/teams/{team}/repos", json={"repoId": rid}, headers=alice.auth)

    body = client.get("/v1/team/sessions", headers=carol.auth).text
    assert "sess-alice-5" in body and "sess-bob-4" in body


def test_the_repo_listing_does_not_confirm_a_guessed_remote(client, alice, bob):
    """The last place the existence oracle could still be read.

    Scoping the publish check is not enough on its own: `GET /v1/team/repos`
    serves the SHARED registry row, so a guesser who publishes a
    deliberately wrong `name`, publishes one row, and reads the listing sees
    their own name back if the repo was new and somebody else's if it was
    not. That difference is the same answer the 409 used to give.

    So a caller who reaches a repo only as its publisher is served what they
    themselves sent. The two responses below must be identical apart from
    the ids, whether or not Alice got there first.
    """
    taken = _publish_repo(client, alice, SECRET_REMOTE)
    _publish_session(client, alice, taken, "sess-alice-6", "main")
    _publish_span(client, alice, "sess-alice-6", "sp-alice-6")

    def probe(remote, rid, session_id):
        client.post("/v1/team/publish", headers=bob.auth, json={
            "kind": "repos", "rows": [{
                "repoId": rid, "remoteUrl": remote, "forge": "bobforge",
                "owner": "bob-says", "repo": "bob-says",
                "webUrl": "https://bob.example/x", "name": "BOB'S OWN LABEL"}]})
        _publish_session(client, bob, rid, session_id, "main")
        _publish_span(client, bob, session_id, f"sp-{session_id}")
        return next(r for r in
                    client.get("/v1/team/repos", headers=bob.auth).json()["repos"]
                    if r["repoId"] == rid)

    contested = probe(SECRET_REMOTE, taken, "sess-probe-1")
    fresh = probe("https://github.com/acme/never-seen", 
                  repo_id("https://github.com/acme/never-seen"), "sess-probe-2")

    for field in ("forge", "owner", "repo", "webUrl", "name"):
        assert contested[field] == fresh[field], (
            f"{field} differed, which answers 'does anybody here work on this repo'"
        )
    assert contested["name"] == "BOB'S OWN LABEL"

    # And Alice, who published it first, is unaffected by anything Bob said.
    hers = next(r for r in client.get("/v1/team/repos", headers=alice.auth).json()["repos"]
                if r["repoId"] == taken)
    assert hers["name"] == "payments" and hers["forge"] == "github"
