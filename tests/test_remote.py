"""`remote.py` unit tests: the parts a live server cannot be made to produce.

`test_remote_e2e.py` is where the client is proved against the real thing,
and it is the one that matters. This file covers what a real server will not
do on request -- a 503, a body that is not JSON, a `Retry-After` header with
rubbish in it -- plus the pure functions, where a socket would only make the
test slower and less specific.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from cc_insights import db, remote, sync


class _Response(io.BytesIO):
    """Just enough of an HTTP response for `_decode`."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def opener(status: int = 200, body=None, headers: dict | None = None,
           *, raises: Exception | None = None):
    """A transport that answers with exactly one thing, and records the request."""
    seen: list = []

    def _open(req, timeout):
        seen.append(req)
        if raises is not None:
            raise raises
        payload = b"" if body is None else json.dumps(body).encode()
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "nope", headers or {}, io.BytesIO(payload)
            )
        return _Response(payload)

    _open.seen = seen
    return _open


def client(**kw):
    return remote.Client("https://example.test", "ccis_token",
                         opener=opener(**kw))


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


def test_a_401_is_an_auth_error_naming_the_command():
    """Every other failure mode in this file is something a user waits out.
    This is the one they can act on, and the action has to be in the text."""
    with pytest.raises(remote.AuthRequired) as caught:
        client(status=401, body={"error": "unauthenticated", "message": "no"}).whoami()
    assert "cci login" in str(caught.value)
    assert caught.value.status == 401


def test_a_404_is_not_reported_as_a_permission_problem():
    """§0: 404 also means "exists, and is not yours". Saying "you lack
    permission" would confirm the row exists, which is the whole thing the
    status code was chosen to avoid."""
    with pytest.raises(remote.NotFound) as caught:
        client(status=404, body={"error": "not_found", "message": "No such team."}).teams()
    message = str(caught.value)
    assert "permission" not in message.lower()
    assert "No such team." in message


def test_a_503_reads_as_transient_and_says_capture_is_unaffected():
    """A database outage on the server must not read as "your install is
    broken" -- the two have completely different responses."""
    with pytest.raises(remote.Unreachable) as caught:
        client(status=503, body={"error": "database_unavailable", "message": "x"}).whoami()
    assert "local capture is unaffected" in str(caught.value)


def test_a_502_is_retryable_rather_than_terminal():
    """GitHub being unreachable is not the client's problem and is usually
    over in a minute. A client that treats it as fatal makes the user redo a
    sign-in that would have worked."""
    with pytest.raises(remote.Unreachable) as caught:
        client(status=502, body={"error": "upstream_unavailable",
                                 "message": "GitHub could not be reached."}).whoami()
    assert caught.value.code == "upstream_unavailable"
    assert "Try again" in str(caught.value)


def test_a_429_carries_the_wait_from_the_header():
    """`Retry-After` is the only thing that tells a client how long to back
    off; ignoring it is how one CLI keeps a whole instance rate limited."""
    with pytest.raises(remote.RemoteError) as caught:
        client(status=429, body={"error": "rate_limited", "message": "slow"},
               headers={"Retry-After": "12"}).whoami()
    assert caught.value.retry_after == 12
    assert "12s" in str(caught.value)


def test_a_nonsense_retry_after_does_not_crash_the_client():
    """A header a proxy mangled must cost a missing number, not a traceback
    on top of the error the user was already being told about."""
    with pytest.raises(remote.RemoteError) as caught:
        client(status=429, body={"error": "rate_limited", "message": "slow"},
               headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}).whoami()
    assert caught.value.retry_after is None


def test_a_body_that_is_not_json_still_produces_a_sentence():
    """A proxy returning an HTML error page must not turn into a
    JSONDecodeError with no mention of what was being attempted."""
    def _open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {},
                                     io.BytesIO(b"<html>502 Bad Gateway</html>"))
    with pytest.raises(remote.RemoteError) as caught:
        remote.Client("https://example.test", "t", opener=_open).whoami()
    assert "HTTP 500" in str(caught.value)


def test_a_dead_socket_is_unreachable_not_a_urllib_error():
    """`urllib.error.URLError` in a user's terminal is a bug report waiting
    to happen about a network they already know is down."""
    bad = remote.Client("https://example.test", "t",
                        opener=opener(raises=urllib.error.URLError("no route")))
    with pytest.raises(remote.Unreachable) as caught:
        bad.whoami()
    assert "cannot reach the account server" in str(caught.value)


def test_no_token_never_reaches_the_transport():
    """Asking the server whether we are signed in is a slower way to print
    the same sentence, and it fails differently when the server is down."""
    transport = opener()
    with pytest.raises(remote.AuthRequired):
        remote.Client("https://example.test", None, opener=transport).whoami()
    assert transport.seen == [], "no request may be made without a credential"


def test_the_token_is_sent_as_a_bearer_header():
    """A token in a query string lands in every access log between here and
    the server."""
    transport = opener(body={"accountId": "acc_1"})
    remote.Client("https://example.test", "ccis_secret", opener=transport).whoami()
    req = transport.seen[0]
    assert req.get_header("Authorization") == "Bearer ccis_secret"
    assert "ccis_secret" not in req.full_url


# --------------------------------------------------------------------------
# the device flow, driven without a clock
# --------------------------------------------------------------------------


def scripted(responses):
    """A transport that returns each response in turn."""
    queue = list(responses)

    def _open(req, timeout):
        status, body, headers = queue.pop(0)
        payload = json.dumps(body).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "e", headers or {},
                                         io.BytesIO(payload))
        return _Response(payload)

    return _open


def _flow(interval: int = 5) -> remote.DeviceFlow:
    return remote.DeviceFlow("dev_x", "WDJB-MJHT", "https://gh/device",
                             "https://gh/device?user_code=WDJB-MJHT",
                             9_999_999_999_999, interval)


def test_the_interval_only_ever_rises():
    """RFC 8628 §3.5 and the contract both say the back-off is sticky. A
    client that drops back to its original interval re-earns the raise, and
    the server's step is 5 seconds each time -- so it converges upward
    whatever the client does, just slowly and noisily."""
    transport = scripted([
        (400, {"error": "slow_down", "message": "x", "interval": 10,
               "retryAfter": 10}, {"Retry-After": "10"}),
        (400, {"error": "authorization_pending", "message": "x", "interval": 10}, {}),
        (200, {"accessToken": "ccis_t", "accountId": "acc_1", "actor": "alice",
               "expiresAt": None}, {}),
    ])
    waits: list[float] = []
    identity = remote.Client("https://example.test", None,
                             opener=transport).device_await(
        _flow(interval=5), sleep=waits.append)

    assert identity.actor == "alice"
    assert len(waits) == 2
    assert waits[0] >= 10, "must adopt the raised interval"
    assert waits[1] >= 10, "and must not drop back to 5 afterwards"


def test_an_unknown_error_is_not_treated_as_pending():
    """The contract is explicit: an unrecognised GitHub error is never
    translated into `authorization_pending`, because a client that retries
    forever on an unknown error never tells its user what went wrong."""
    transport = scripted([
        (502, {"error": "upstream_unavailable",
               "message": "GitHub returned an unexpected error: 'wat'"}, {}),
        (400, {"error": "expired_token", "message": "gone"}, {}),
    ])
    with pytest.raises(remote.DeviceFlowError) as caught:
        remote.Client("https://example.test", None, opener=transport).device_await(
            _flow(), sleep=lambda _s: None)
    assert caught.value.code == remote.EXPIRED


def test_a_flow_that_has_already_expired_stops_without_polling_forever():
    """The client keeps its own deadline as a backstop. Without it, a server
    that stops answering turns `cci login` into a command that never
    returns."""
    already = remote.DeviceFlow("dev_x", "C", "u", "u", 1, 5)
    transport = scripted([
        (400, {"error": "authorization_pending", "message": "x"}, {}),
    ])
    with pytest.raises(remote.DeviceFlowError) as caught:
        remote.Client("https://example.test", None, opener=transport).device_await(
            already, sleep=lambda _s: None)
    assert caught.value.code == remote.EXPIRED


# --------------------------------------------------------------------------
# pure functions
# --------------------------------------------------------------------------


def test_set_fragments_round_trips_every_custom_clause():
    """`remote` narrows these to the columns actually sent. If splitting and
    rejoining is not lossless, the narrowed UPDATE is silently different from
    the one `sync` runs, and the two transports stop agreeing."""
    for table in sync.TABLES:
        fragments = sync.set_fragments(table)
        if not table.set_clause:
            assert fragments == {}
            continue
        assert ", ".join(fragments.values()) == table.set_clause
        for column in fragments:
            assert column in table.updatable, f"{table.name}.{column} is not updatable"


def test_a_narrowed_upsert_leaves_the_columns_it_did_not_receive_alone():
    """The contract's "an absent optional column is left untouched rather
    than nulled" is implemented as the only thing it honestly can be: not
    mentioning the column."""
    host = {t.name: t for t in sync.TABLES}["host"]
    statement = remote.wire_upsert_sql(
        host, ["host_id", "hostname", "os", "first_seen", "last_seen"])
    assert "account_id" not in statement, (
        "a column that did not arrive must not be written"
    )
    assert "excluded.hostname" in statement
    assert "first_seen" in statement


def test_a_narrowed_upsert_is_valid_sql_for_every_table():
    """A clause built by string surgery either runs or it does not, and
    finding out during somebody's first pull is too late."""
    conn = db.connect(_tmpdb())
    db.migrate(conn)
    try:
        for table in sync.TABLES:
            columns = [c for c in table.columns if c != "account_id"]
            statement = remote.wire_upsert_sql(table, columns)
            conn.execute(f"EXPLAIN {statement}", [None] * len(columns))
    finally:
        conn.close()


def _tmpdb():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "t.db"


SERVER_TABLES = {
    "tables": [
        {"table": t.name,
         "key": list(t.key),
         "columns": [c for c in t.columns if (t.name, c) not in remote.WIRE_OMITTED],
         "optionalColumns": [],
         "hostScoped": t.owner_filter is not None}
        for t in sync.TABLES
    ],
    "excluded": [],
    "maxBatchRows": 5000,
}


def test_the_plan_drops_only_columns_with_a_written_reason():
    """"Never a silently dropped value" is the rule the server follows on its
    side. A client that quietly omits a column is the same defect facing the
    other way."""
    plans = {p.name: p for p in remote.plan_tables(SERVER_TABLES)}
    assert plans["host"].omitted == ("account_id",)
    assert ("host", "account_id") in remote.WIRE_OMITTED
    for name, plan in plans.items():
        for column in plan.omitted:
            assert (name, column) in remote.WIRE_OMITTED


def test_an_unexplained_missing_column_is_drift_not_a_quiet_drop():
    """The failure this exists for: a server that stops modelling a column
    the client still has. Dropping it would lose data with no signal."""
    narrowed = json.loads(json.dumps(SERVER_TABLES))
    for spec in narrowed["tables"]:
        if spec["table"] == "session":
            spec["columns"].remove("git_branch")
    with pytest.raises(remote.SchemaDrift, match="git_branch"):
        remote.plan_tables(narrowed)


def test_a_column_the_client_lacks_is_drift_and_names_the_upgrade():
    """The other direction: a newer server requiring something this client
    cannot produce. Pushing a subset would upsert NULLs over real data."""
    widened = json.loads(json.dumps(SERVER_TABLES))
    for spec in widened["tables"]:
        if spec["table"] == "span":
            spec["columns"].append("brand_new_column")
    with pytest.raises(remote.SchemaDrift, match="Upgrade cc-insights"):
        remote.plan_tables(widened)


def test_a_table_the_server_does_not_transfer_is_drift():
    """Silently skipping it would mean a push that reports success and leaves
    a table behind."""
    trimmed = {"tables": [t for t in SERVER_TABLES["tables"] if t["table"] != "span"]}
    with pytest.raises(remote.SchemaDrift, match="span"):
        remote.plan_tables(trimmed)


def test_an_unknown_team_filter_raises_rather_than_being_ignored():
    """A filter that silently does nothing answers a different question than
    the one that was asked, with numbers that look right."""
    with pytest.raises(ValueError, match="unknown team filter"):
        remote._filter_params({"repoo": "x"})


def test_repeated_filters_become_repeated_query_parameters():
    """§4.3 filters are repeatable and intersecting; collapsing a list into
    one comma-joined value silently matches nothing."""
    params = remote._filter_params({"repo": ["a", "b"], "frm": 5})
    assert ("repo", "a") in params and ("repo", "b") in params
    assert ("from", 5) in params, "`from` is a Python keyword and `frm` on the way in"


# --------------------------------------------------------------------------
# transfer state
# --------------------------------------------------------------------------


def test_state_round_trips(tmp_path):
    """Losing the resume point costs a re-push of 191k rows."""
    state = remote.load_state(tmp_path)
    key = remote.target_key("https://a.test/", "acc_1")
    state.setdefault("targets", {})[key] = {"push": {"lastSuccessAt": 42}}
    remote.save_state(tmp_path, state)
    assert remote.last_push_at(tmp_path, "https://a.test", "acc_1") == 42


def test_state_is_keyed_by_server_and_account(tmp_path):
    """Signing in as somebody else must not inherit the previous account's
    "already pushed" marks, or the new account's first push sends nothing."""
    remote.record_publish(tmp_path, "https://a.test", "acc_1", confirmed=True)
    assert remote.has_published(tmp_path, "https://a.test", "acc_1")
    assert not remote.has_published(tmp_path, "https://a.test", "acc_2")
    assert not remote.has_published(tmp_path, "https://b.test", "acc_1")


def test_a_corrupt_state_file_costs_a_re_push_and_not_a_crash(tmp_path):
    """Every id is a content hash, so forgetting where we were is survivable.
    Refusing to run because a bookkeeping file is unreadable is not."""
    remote.state_path(tmp_path).write_text("{not json at all")
    assert remote.load_state(tmp_path) == {"version": remote.STATE_VERSION, "targets": {}}
    assert remote.last_push_at(tmp_path, "https://a.test", "acc_1") is None


def test_a_state_file_from_a_future_version_is_ignored_not_misread(tmp_path):
    """Reading an unknown layout as if it were this one would resume from a
    cursor that means something else."""
    remote.state_path(tmp_path).write_text(json.dumps({"version": 999, "targets": {
        remote.target_key("https://a.test", "acc_1"): {"push": {"lastSuccessAt": 7}}}}))
    assert remote.last_push_at(tmp_path, "https://a.test", "acc_1") is None


def test_the_row_digest_separates_null_from_empty_string():
    """If they hashed alike, clearing a column would look like no change and
    the table would be skipped -- forever, because the digest would keep
    matching."""
    assert remote._row_bytes([None, "a"]) != remote._row_bytes(["", "a"])
    assert remote._row_bytes(["a", "b"]) != remote._row_bytes(["ab", ""])
