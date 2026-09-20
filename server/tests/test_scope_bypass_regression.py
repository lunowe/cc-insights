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

from conftest import session_row  # noqa: F401  (kept for parity with siblings)

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
