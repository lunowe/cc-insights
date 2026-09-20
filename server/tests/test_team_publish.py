"""Publishing a projection: what is accepted, what is refused, and why.

`redact.publication()` builds this on the laptop. The server's job is to be
the second place that says no -- to a mis-stamped actor, to a fourth
`thread_role`, to a span attached to somebody else's session, and to anything
that looks like a path.
"""

from __future__ import annotations

from cci_server.repoid import github_remote, repo_id

R1 = repo_id(github_remote("github.com", "acme/app"))
T0 = 1_789_000_000_000


def repos(actor, rid=R1, name="app"):
    return {"kind": "repos", "actor": actor,
            "rows": [{"repoId": rid, "remoteUrl": f"https://github.com/acme/{name}",
                      "forge": "github", "owner": "acme", "repo": name,
                      "webUrl": f"https://github.com/acme/{name}", "name": name}]}


def sessions(actor, sid="s1", rid=R1, branch="main"):
    return {"kind": "sessions", "actor": actor,
            "rows": [{"sessionId": sid, "repoId": rid, "actor": actor,
                      "source": "codex", "gitBranch": branch, "startedAt": T0,
                      "endedAt": T0 + 60_000, "activeMs": 60_000, "eventCount": 4}]}


def spans(actor, span_id="sp1", sid="s1", role="human"):
    return {"kind": "spans", "actor": actor,
            "rows": [{"spanId": span_id, "sessionId": sid, "threadRole": role,
                      "startedAt": T0, "endedAt": T0 + 60_000, "eventCount": 4}]}


def test_publishing_the_same_projection_twice_is_a_no_op(client, alice):
    """Span ids hash a thread id and a timestamp, so a re-publish must collapse.

    `cci privacy` and a scheduled publish will both run over the same corpus.
    If either duplicated rows, every team total would climb on a schedule.
    """
    for _ in range(2):
        assert client.post("/v1/team/publish", json=repos(alice.actor),
                           headers=alice.auth).status_code == 200
        assert client.post("/v1/team/publish", json=sessions(alice.actor),
                           headers=alice.auth).status_code == 200
        assert client.post("/v1/team/publish", json=spans(alice.actor),
                           headers=alice.auth).status_code == 200

    body = client.get("/v1/team/summary", headers=alice.auth).json()
    assert body["spans"] == 1
    assert body["sessions"] == 1
    assert body["activeMs"] == 60_000


def test_publishing_under_somebody_elses_name_is_refused(client, alice, bob):
    """The team view's central claim is that it says who did the work.

    If a client could choose the actor, that claim is unenforced and a week
    of somebody's time could be filed under a colleague.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    r = client.post("/v1/team/publish", json=sessions("bob"), headers=alice.auth)
    assert r.status_code == 403
    assert r.json()["error"] == "actor_mismatch"

    # And the batch-level actor is checked too, not only the row-level one.
    body = sessions(alice.actor)
    body["actor"] = "bob"
    assert client.post("/v1/team/publish", json=body,
                       headers=alice.auth).status_code == 403


def test_the_stored_actor_comes_from_the_token(client, alice, migrated_db):
    """Even the accepted value is re-stamped from the account, not copied.

    A client that sends the right name still must not be the source of it:
    the row is written from `account.actor`, so there is one authority.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    with migrated_db.connection() as conn:
        row = conn.execute(
            "SELECT actor, account_id FROM published_session"
        ).fetchone()
    assert row["actor"] == "alice"
    assert row["account_id"] == alice.account_id


def test_a_span_cannot_be_attached_to_another_accounts_session(client, alice, bob):
    """Session ids are unguessable, so this should never fire.

    It is checked anyway, because "should never" is exactly how the
    confirmation attack in docs/REDACTION.md §0 survived into a design. Bob
    naming Alice's session must not write a span into her data.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)

    r = client.post("/v1/team/publish", json=spans(bob.actor, span_id="sp-bob"),
                    headers=bob.auth)
    assert r.status_code == 200
    assert r.json()["applied"] == 0
    assert r.json()["rejected"] == 1

    assert client.get("/v1/team/summary", headers=alice.auth).json()["spans"] == 0


def test_a_session_already_published_by_someone_else_is_refused_not_merged(
    client, alice, bob, migrated_db
):
    """Silently taking the newer write is how one account's row becomes another's."""
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)

    # Bob registers the repo under his own account first. The order check in
    # `_publish_sessions` is per-caller now -- asking `published_repo`
    # globally made a 200-vs-409 answer "does anybody here work on this
    # repo" for a guessed id -- so Bob reaching a repo Alice created is the
    # publish this test is about, not an accident of shared registry state.
    client.post("/v1/team/publish", json=repos(bob.actor), headers=bob.auth)
    r = client.post("/v1/team/publish", json=sessions(bob.actor, sid="s1"),
                    headers=bob.auth)
    assert r.json()["rejected"] == 1
    with migrated_db.connection() as conn:
        row = conn.execute("SELECT actor FROM published_session").fetchone()
    assert row["actor"] == "alice"


def test_publishing_out_of_order_is_a_409_naming_what_is_missing(client, alice):
    """A client that gets the order wrong needs the remedy, not a stack trace."""
    r = client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    assert r.status_code == 409
    assert r.json()["error"] == "foreign_key_violation"
    assert "repos" in r.json()["message"]

    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    late = client.post("/v1/team/publish", json=spans(alice.actor, sid="s-nope"),
                       headers=alice.auth)
    assert late.status_code == 409
    assert "sessions" in late.json()["message"]


def test_a_fourth_thread_role_is_refused(client, alice):
    """The three buckets partition active time; a fourth makes totals stop adding up.

    `stats.py` computes exactly `human` / `autonomous` / `unattended_root`,
    and the label travels so the partition survives aggregation. A typo in a
    client would otherwise create a bucket nothing sums.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    r = client.post("/v1/team/publish", json=spans(alice.actor, role="human_ish"),
                    headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_thread_role"


def test_the_role_breakdown_partitions_the_total(client, alice):
    """`byRole` must add up to `activeMs`, or the three-bucket label bought nothing."""
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    for i, role in enumerate(("human", "autonomous", "unattended_root")):
        client.post("/v1/team/publish", json=spans(alice.actor, f"sp{i}", role=role),
                    headers=alice.auth)
    body = client.get("/v1/team/summary", headers=alice.auth).json()
    assert sum(body["byRole"].values()) == body["activeMs"]
    assert body["activeMs"] == 3 * 60_000


def test_a_publish_body_carrying_a_path_field_is_simply_dropped(client, alice,
                                                                migrated_db):
    """There is nowhere for a path to go, which is the point of migration 003.

    A client bug -- or a future version that forgot the projection -- sending
    `rootPath` alongside a session must not result in it being stored. The
    schema has no column, so the field has no destination, and the row lands
    without it.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    body = sessions(alice.actor)
    body["rows"][0]["rootPath"] = "/Users/alice/Coding/AcmeCorp-Unreleased"
    body["rows"][0]["cwd"] = "/Users/alice/Coding/AcmeCorp-Unreleased/api"
    assert client.post("/v1/team/publish", json=body,
                       headers=alice.auth).status_code == 200

    with migrated_db.connection() as conn:
        for table in ("published_session", "published_repo",
                      "published_session_branch", "published_span"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert "AcmeCorp-Unreleased" not in str(rows), table


def test_dropping_a_branch_name_on_republish_removes_the_stored_one(client, alice,
                                                                    migrated_db):
    """A client that stops sending branches must actually stop publishing them.

    Freezing the last value sent would mean turning the feature off locally
    left the old names in the store, visible forever.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor, branch="feat/x"),
                headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor, branch=None),
                headers=alice.auth)

    with migrated_db.connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM published_session_branch"
        ).fetchone()["n"] == 0


def test_an_unknown_publish_kind_is_refused(client, alice):
    """Silently accepting an unknown kind would report success for nothing sent."""
    r = client.post("/v1/team/publish", json={"kind": "events", "rows": []},
                    headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_kind"


def test_a_publish_above_the_cap_is_refused(client, alice):
    """Re-sending an overlapping range costs nothing, so splitting is always safe."""
    from cci_server.config import MAX_BATCH_ROWS

    body = repos(alice.actor)
    body["rows"] = body["rows"] * (MAX_BATCH_ROWS + 1)
    r = client.post("/v1/team/publish", json=body, headers=alice.auth)
    assert r.status_code == 413


def test_publishing_requires_a_token(client, alice):
    """The whole endpoint, not merely its contents."""
    assert client.post("/v1/team/publish", json=repos("alice")).status_code == 401


R2 = repo_id(github_remote("github.com", "acme/private"))


def test_a_session_that_moves_repos_takes_its_spans_with_it(client, alice, migrated_db):
    """Migration 003 claimed these "cannot disagree with the session". They could.

    `_publish_sessions` upserts `repo_id = excluded.repo_id`, so a session
    moves repos whenever a checkout's remote is corrected -- and `redact.py`
    re-normalizes every remote on every run BY DESIGN, so one fix to
    `normalize_remote` moves them in bulk. Nothing propagated that to the
    spans already stored: no trigger, no cascade, no test.

    Since every scope predicate in `team_data.py` sits on `sp.repo_id`, a
    stale span means a viewer scoped to the OLD repo reads a session that now
    belongs to the NEW one -- and the response body carries the new, private
    `repo_id` with it.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=repos(alice.actor, rid=R2, name="private"),
                headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=spans(alice.actor), headers=alice.auth)

    with migrated_db.connection() as conn:
        assert conn.execute(
            "SELECT repo_id FROM published_span WHERE span_id = 's1p1' OR span_id = 'sp1'"
        ).fetchone()["repo_id"] == R1

    # The remote was corrected; the same session is now under a different id.
    moved = client.post("/v1/team/publish", json=sessions(alice.actor, rid=R2),
                        headers=alice.auth)
    assert moved.status_code == 200, moved.text

    with migrated_db.connection() as conn:
        rows = conn.execute(
            """SELECT sp.span_id, sp.repo_id AS span_repo, s.repo_id AS session_repo
               FROM published_span sp
               JOIN published_session s ON s.session_id = sp.session_id"""
        ).fetchall()
        assert rows, "the span vanished instead of moving"
        for r in rows:
            assert r["span_repo"] == r["session_repo"] == R2, r["span_id"]
        branch = conn.execute(
            "SELECT repo_id FROM published_session_branch"
        ).fetchone()
        assert branch["repo_id"] == R2, "the branch row kept the old repo"


def test_a_viewer_scoped_to_the_old_repo_stops_seeing_a_moved_session(
    client, alice, bob, migrated_db
):
    """The disclosure the divergence actually caused, as a request.

    Bob has verified access to the public repo and none to the private one.
    A span left behind under the public id kept him reading a session that
    had moved, private `repoId` included.
    """
    client.post("/v1/team/publish", json=repos(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=repos(alice.actor, rid=R2, name="private"),
                headers=alice.auth)
    client.post("/v1/team/publish", json=sessions(alice.actor), headers=alice.auth)
    client.post("/v1/team/publish", json=spans(alice.actor), headers=alice.auth)

    with migrated_db.connection() as conn:
        conn.execute(
            """INSERT INTO account_repo_access (account_id, repo_id, provider, verified_at)
               VALUES (%s, %s, 'github', 1)""",
            (bob.account_id, R1),
        )
    assert client.get("/v1/team/sessions", headers=bob.auth).json()["sessions"]

    client.post("/v1/team/publish", json=sessions(alice.actor, rid=R2),
                headers=alice.auth)

    body = client.get("/v1/team/sessions", headers=bob.auth)
    assert body.json()["sessions"] == []
    assert R2 not in body.text, "the private repo_id reached a viewer without access"
