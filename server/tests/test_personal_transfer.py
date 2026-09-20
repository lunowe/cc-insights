"""Push and pull: idempotency, batching, resume, and the guards on a row.

The measured corpus is 191,475 rows, so the transfer has to batch and resume.
Idempotency is free -- every id is already a content hash -- which is a claim
worth testing rather than repeating.
"""

from __future__ import annotations

from conftest import host_row, project_row, session_row

SPAN_COLUMNS = ["id", "session_id", "thread_id", "started_at", "ended_at",
                "event_count", "attended"]
THREAD_COLUMNS = ["id", "native_id", "session_id", "parent_thread_id", "is_subagent",
                  "agent_name", "started_at", "ended_at", "event_count", "active_ms"]


def _seed(client, who, host="h1", project="p1", session="s1"):
    client.post("/v1/personal/push", json=host_row(host), headers=who.auth)
    client.post("/v1/personal/push", json=project_row(host, project, "/tmp/x"),
                headers=who.auth)
    client.post("/v1/personal/push", json=session_row(host, session, project),
                headers=who.auth)


def _threads(host, session, rows):
    return {"hostId": host, "table": "thread", "columns": THREAD_COLUMNS, "rows": rows}


def test_pushing_the_same_batch_twice_changes_nothing(client, alice):
    """Every id is a content hash, so a re-push must collapse, not duplicate.

    This is the property the whole transport rests on: a client that crashes
    mid-push resumes by sending an overlapping range, and if that duplicated
    rows every total on the dashboard would drift upward on every retry.
    """
    _seed(client, alice)
    before = client.get("/v1/personal/status", headers=alice.auth).json()

    _seed(client, alice)
    after = client.get("/v1/personal/status", headers=alice.auth).json()

    assert before["tables"] == after["tables"]
    assert after["totalRows"] == 3


def test_a_re_push_does_not_move_first_seen_backwards_or_last_seen_back(client, alice):
    """`sync.TABLES` keeps min(first_seen) and max(last_seen); so must this.

    A later push from a machine that was reinstalled would otherwise report
    that it had existed only since yesterday, silently truncating the history
    every chart is drawn from.
    """
    early = host_row("h1")
    early["rows"][0][3] = 1_000_000_000_000  # first_seen
    early["rows"][0][4] = 1_900_000_000_000  # last_seen
    client.post("/v1/personal/push", json=early, headers=alice.auth)

    later = host_row("h1")
    later["rows"][0][3] = 1_500_000_000_000
    later["rows"][0][4] = 1_600_000_000_000
    client.post("/v1/personal/push", json=later, headers=alice.auth)

    row = client.get("/v1/personal/pull?table=host", headers=alice.auth).json()["rows"][0]
    assert row[3] == 1_000_000_000_000
    assert row[4] == 1_900_000_000_000


def test_a_pin_survives_a_push_from_a_machine_that_was_never_told(client, alice):
    """A pin is a human saying where a project belongs.

    `cci group auto` must never move it, and neither may another machine's
    push -- which would silently undo a correction somebody made by hand.
    """
    client.post("/v1/personal/push", json=host_row("h1"), headers=alice.auth)
    grp = {
        "hostId": "h1", "table": "project_group",
        "columns": ["group_id", "name", "origin", "match_key", "remote_url", "forge",
                    "owner", "repo", "web_url", "created_at", "updated_at"],
        "rows": [["g-pinned", "Chosen", "manual", "k1", None, None, None, None, None,
                  1, 1],
                 ["g-auto", "Detected", "git_remote", "k2",
                  "https://github.com/a/b", "github", "a", "b",
                  "https://github.com/a/b", 1, 1]],
    }
    client.post("/v1/personal/push", json=grp, headers=alice.auth)

    pinned = project_row("h1", "p1", "/tmp/x")
    pinned["rows"][0][3] = "g-pinned"
    pinned["rows"][0][4] = 1
    client.post("/v1/personal/push", json=pinned, headers=alice.auth)

    unaware = project_row("h1", "p1", "/tmp/x")
    unaware["rows"][0][3] = "g-auto"
    unaware["rows"][0][4] = 0
    client.post("/v1/personal/push", json=unaware, headers=alice.auth)

    row = client.get("/v1/personal/pull?table=project", headers=alice.auth).json()["rows"][0]
    assert row[3] == "g-pinned"
    assert row[4] == 1


def test_a_child_thread_may_arrive_before_its_parent_in_one_batch(client, alice):
    """Subagents nest, so a batch contains children whose parents are in it.

    Without the deferred self-reference in migration 002, whether a push
    succeeds would depend on the order rows happened to come back in -- a
    failure that only reproduces on somebody else's machine.
    """
    _seed(client, alice)
    rows = [
        ["t-child", "n2", "s1", "t-parent", 1, "explorer", 10, 20, 2, 10],
        ["t-parent", "n1", "s1", None, 0, None, 0, 30, 5, 30],
    ]
    r = client.post("/v1/personal/push", json=_threads("h1", "s1", rows),
                    headers=alice.auth)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2


def test_pushing_a_span_before_its_session_is_a_409_not_a_500(client, alice):
    """A client that gets the order wrong needs to be told what to do.

    A 500 says "file a bug"; a 409 naming the table says "push the parent
    first", which is the actual remedy.
    """
    client.post("/v1/personal/push", json=host_row("h1"), headers=alice.auth)
    r = client.post("/v1/personal/push", json={
        "hostId": "h1", "table": "span", "columns": SPAN_COLUMNS,
        "rows": [["sp1", "missing", "missing", 1, 2, 1, 1]],
    }, headers=alice.auth)
    assert r.status_code == 409
    assert r.json()["error"] == "foreign_key_violation"


def test_a_failed_batch_lands_nothing(client, alice):
    """A half-applied batch is sessions without their events.

    Every count on the dashboard would then be wrong in a way that looks
    entirely plausible, which is worse than an error.
    """
    _seed(client, alice)
    r = client.post("/v1/personal/push", json={
        "hostId": "h1", "table": "span", "columns": SPAN_COLUMNS,
        "rows": [["sp-ok", "s1", "t-none", 1, 2, 1, 1],
                 ["sp-bad", "nope", "nope", 1, 2, 1, 1]],
    }, headers=alice.auth)
    assert r.status_code == 409
    assert client.get("/v1/personal/pull?table=span",
                      headers=alice.auth).json()["rows"] == []


def test_a_missing_column_is_refused_rather_than_written_as_null(client, alice):
    """A partial row would upsert NULLs over data that is already stored.

    `cwd` and `git_branch` would quietly become NULL on every row a client
    with an old column list touched.
    """
    body = host_row("h1")
    body["columns"] = body["columns"][:3]
    body["rows"] = [r[:3] for r in body["rows"]]
    r = client.post("/v1/personal/push", json=body, headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "missing_column"


def test_an_unknown_column_is_refused_rather_than_dropped(client, alice):
    """A silently dropped value is a value the client thinks it sent."""
    body = host_row("h1")
    body["columns"].append("prompt_text")
    body["rows"][0].append("you are a helpful assistant")
    r = client.post("/v1/personal/push", json=body, headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "unknown_column"


def test_an_excluded_table_says_why_it_is_excluded(client, alice):
    """`sync.EXCLUDED` carries the reason, and the reason is what stops a re-add."""
    r = client.post("/v1/personal/push", json={
        "hostId": "h1", "table": "ingest_file", "columns": ["host_id"], "rows": [],
    }, headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "table_not_transferred"
    assert "full local path" in r.json()["message"]


def test_a_nested_value_is_rejected(client, alice):
    """Metadata only. A nested object is somewhere a transcript could ride along.

    PostgreSQL would happily store its JSON encoding in a TEXT column, and
    nothing downstream would ever look.
    """
    body = host_row("h1")
    body["rows"][0][1] = {"messages": [{"role": "user", "content": "..."}]}
    r = client.post("/v1/personal/push", json=body, headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_value"


def test_a_very_long_string_is_rejected(client, alice):
    """Nothing in this schema is prose. A 40 kB `hostname` is a transcript.

    "No prompt text in the schema" has held because it is enforced where rows
    are written; a server that accepts arbitrary strings is where it stops.
    """
    body = host_row("h1")
    body["rows"][0][1] = "x" * 40_000
    r = client.post("/v1/personal/push", json=body, headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "value_too_long"


def test_a_batch_above_the_cap_is_refused_with_the_cap_in_the_body(client, alice):
    """A client needs the number, not a guess, to split its batches."""
    from cci_server.config import MAX_BATCH_ROWS

    body = host_row("h1")
    body["rows"] = body["rows"] * (MAX_BATCH_ROWS + 1)
    r = client.post("/v1/personal/push", json=body, headers=alice.auth)
    assert r.status_code == 413
    assert r.json()["maxRows"] == MAX_BATCH_ROWS


def test_pull_pages_with_a_keyset_cursor_and_covers_every_row(client, alice):
    """Resume has to be exact: a skipped row is a session nobody notices is gone.

    OFFSET would shift under a concurrent insert. A keyset cursor does not,
    which is the property being relied on here and the reason the last page
    is signalled by `nextCursor: null` rather than by a short page.
    """
    _seed(client, alice)
    rows = [["t%03d" % i, "n%d" % i, "s1", None, 0, None, i, i + 1, 1, 1]
            for i in range(25)]
    assert client.post("/v1/personal/push", json=_threads("h1", "s1", rows),
                       headers=alice.auth).status_code == 200

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        url = "/v1/personal/pull?table=thread&limit=10"
        if cursor:
            url += f"&cursor={cursor}"
        body = client.get(url, headers=alice.auth).json()
        seen.extend(r[0] for r in body["rows"])
        pages += 1
        cursor = body["nextCursor"]
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert len(seen) == 25
    assert len(set(seen)) == 25
    assert pages == 3


def test_a_cursor_from_another_table_is_rejected(client, alice):
    """A replayed cursor must not silently return the wrong page."""
    _seed(client, alice)
    body = client.get("/v1/personal/pull?table=project_probe&limit=1",
                      headers=alice.auth).json()
    del body
    from cci_server import paging

    two_col = paging.encode(["p1", "h1"])  # project_probe's key width
    r = client.get(f"/v1/personal/pull?table=session&cursor={two_col}",
                   headers=alice.auth)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_cursor"


def test_limit_above_the_cap_is_clamped_and_reported(client, alice):
    """Clamped, not rejected: the client asked for more data, not a different thing."""
    _seed(client, alice)
    from cci_server.config import MAX_PAGE

    body = client.get(f"/v1/personal/pull?table=host&limit={MAX_PAGE * 10}",
                      headers=alice.auth).json()
    assert body["limit"] == MAX_PAGE


def test_the_optional_column_may_be_omitted_and_is_not_overwritten(client, alice):
    """`sync.TABLES` has not caught up with migration 005's TTL split.

    A client on the current release omits `cache_write_1h_tokens`. Its push
    must not write NULL over a value a newer client already sent, or 41% of
    the cache-write tokens on this corpus silently revert to being priced as
    five-minute writes.
    """
    _seed(client, alice)
    client.post("/v1/personal/push", json=_threads("h1", "s1", [
        ["t1", "n1", "s1", None, 0, None, 1, 2, 1, 1]]), headers=alice.auth)

    full = ["id", "session_id", "thread_id", "native_event_id", "ts", "ordinal", "kind",
            "model", "tool_name", "tool_use_id", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens", "cache_write_1h_tokens"]
    client.post("/v1/personal/push", json={
        "hostId": "h1", "table": "event", "columns": full,
        "rows": [["e1", "s1", "t1", "ne1", 100, 0, "msg", "m", None, None,
                  1, 2, 3, 400, 150]],
    }, headers=alice.auth)

    older = client.post("/v1/personal/push", json={
        "hostId": "h1", "table": "event", "columns": full[:-1],
        "rows": [["e1", "s1", "t1", "ne1", 100, 0, "msg", "m", None, None,
                  1, 2, 3, 400]],
    }, headers=alice.auth)
    assert older.status_code == 200

    row = client.get("/v1/personal/pull?table=event", headers=alice.auth).json()
    at = row["columns"].index("cache_write_1h_tokens")
    assert row["rows"][0][at] == 150


def test_the_tables_endpoint_describes_the_contract(client, alice):
    """A client should be able to detect drift instead of hardcoding a copy."""
    body = client.get("/v1/personal/tables", headers=alice.auth).json()
    names = [t["table"] for t in body["tables"]]
    assert names[0] == "host" and names.index("project_group") < names.index("project")
    assert names.index("thread") < names.index("event")
    assert {e["table"] for e in body["excluded"]} >= {"ingest_file", "event_cost"}
    assert body["maxBatchRows"] > 0
