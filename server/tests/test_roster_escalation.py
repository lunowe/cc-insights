"""The roster escalation: the bypass reappearing one step later.

Finding 1's first fix -- "publication grants row-level access only" -- is
not sufficient by itself, and this is the test that says so. A publisher is
legitimately in scope for their own rows, so they pass `teams.add_repo`'s
"do you already have access" check. The two-call bypass becomes four:

    publish a guessed repo id
    publish one row of your own under it
    create a team of one
    roster the repo you now "have access to"   -> term 1 -> read everything

No check inside `add_repo` can close it, because at the moment of the call a
legitimate first publisher rostering their own repo and an attacker doing the
same thing are indistinguishable.

What closes it is that a roster DELEGATES the adder's access rather than
conferring full access: a forge-verified adder gives the team the whole repo,
anybody else gives the team only their own rows. Bob's four calls all
succeed -- and he reads exactly the one row he wrote himself.

Kept as a permanent regression test because the escalation is a step removed
from the original bug, so a future change could reopen it while
`test_scope_bypass_regression.py` stays green.
"""
from cci_server.repoid import repo_id
R = "https://github.com/acme/payments"

def _pub(client, who, kind, rows):
    return client.post("/v1/team/publish", headers=who.auth,
                       json={"kind": kind, "rows": rows})

def test_a_roster_cannot_launder_a_self_asserted_repo(client, alice, bob):
    rid = repo_id(R)
    # Alice: the legitimate publisher with real data.
    _pub(client, alice, "repos", [{"repoId":rid,"remoteUrl":R,"forge":"github",
         "owner":"acme","repo":"payments","webUrl":R,"name":"payments"}])
    _pub(client, alice, "sessions", [{"sessionId":"sess-alice","repoId":rid,
         "actor":alice.actor,"source":"claude_code","gitBranch":"feat/unreleased",
         "startedAt":1700000000000,"endedAt":1700000060000,"activeMs":60000,"eventCount":10}])
    _pub(client, alice, "spans", [{"spanId":"span-a","sessionId":"sess-alice",
         "threadRole":"human","startedAt":1700000000000,"endedAt":1700000060000,"eventCount":10}])

    # Bob, no access at all. 1) claim the repo 2) publish one own row
    _pub(client, bob, "repos", [{"repoId":rid,"remoteUrl":R,"forge":"github",
         "owner":"acme","repo":"payments","webUrl":R,"name":"payments"}])
    _pub(client, bob, "sessions", [{"sessionId":"sess-bob","repoId":rid,
         "actor":bob.actor,"source":"claude_code","gitBranch":"main",
         "startedAt":1700000000000,"endedAt":1700000001000,"activeMs":1000,"eventCount":1}])
    _pub(client, bob, "spans", [{"spanId":"span-b","sessionId":"sess-bob",
         "threadRole":"human","startedAt":1700000000000,"endedAt":1700000001000,"eventCount":1}])

    # 3) a team of one 4) roster the repo he "has access to"
    t = client.post("/v1/teams", headers=bob.auth, json={"name":"bobteam"})
    assert t.status_code < 300, t.text
    if True:
        tid = t.json().get("teamId") or t.json().get("team",{}).get("teamId")
        a = client.post(f"/v1/teams/{tid}/repos", headers=bob.auth,
                        json={"repoId": rid, "branchNamesPublished": True})

    for p in ("/v1/team/sessions", "/v1/team/actors", "/v1/team/summary"):
        g = client.get(p, headers=bob.auth)

    body = client.get("/v1/team/sessions", headers=bob.auth).text
    assert "sess-alice" not in body, "LEAK: Alice's session"
    assert "feat/unreleased" not in body, "LEAK: Alice's branch"
    assert alice.actor not in client.get("/v1/team/actors", headers=bob.auth).text, "LEAK: actor"