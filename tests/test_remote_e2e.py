"""`remote.py` against the real account server, over a real socket.

Every test in this file talks to a live uvicorn process, a real PostgreSQL
database and the shipped routes. The one substitution is GitHub, and
`tests/_e2e_server.py` says why that one is not optional.

The alternative -- a stubbed HTTP layer -- would have passed against a client
that sent `host.account_id`, which the server rejects as an unknown column.
That is the whole argument for testing it this way, and it is not
hypothetical: it is the first thing this suite caught.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

import server_harness
from cc_insights import db, redact, remote, sync

ACTOR = "alice"
#: Shaped like the real thing. `config.py` generates `uuid4()` and never
#: regenerates it, and the shape matters to one test here: `host_id` is the
#: only host-derived value the team store receives, so it must not be
#: something that carries a hostname inside it.
HOST_ID = "3f6c1a52-9d47-4b0e-8a11-7c2e5d9f0b31"
LAPTOP_ID = "b81d7e04-2a63-4f9c-9d15-6e30c84a1f77"
HOSTNAME = "studio"
REMOTE_URL = "https://github.com/alice/harbor-cli"

#: A path that must never reach the team store, and whose sha256 IS
#: `project_id`. docs/REDACTION.md §0: 555 guesses recovered 20% of the
#: author's corpus from those ids alone.
ROOT_PATH = "/Users/alice/Coding/SecretClientProject"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live():
    """One server for the module. Booting uvicorn per test is 40x the cost."""
    server = server_harness.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def clean(live):
    live.truncate()
    yield


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway config directory.

    Never the real one: `~/.config/cc-insights` holds a 114 MB database and a
    running launchd job, and `cci login`, `cci publish` and `cci sync` all
    write. Every environment override the commands read is cleared too, so a
    developer's own shell cannot leak into a result.
    """
    where = tmp_path / "cci-home"
    monkeypatch.setenv("CC_INSIGHTS_HOME", str(where))
    monkeypatch.delenv("CC_INSIGHTS_TOKEN", raising=False)
    monkeypatch.delenv("CC_INSIGHTS_SYNC_URL", raising=False)
    monkeypatch.delenv("CC_INSIGHTS_SERVER", raising=False)
    where.mkdir(parents=True, exist_ok=True)
    return where


def _make_db(path, *, events: int = 5, root_path: str = ROOT_PATH,
             host_id: str = HOST_ID, hostname: str = HOSTNAME,
             tag: str = "a") -> sqlite3.Connection:
    """A local database shaped like a real one, with `events` events.

    `tag` prefixes the session, thread, event and span ids. Real ones hash
    `host_id`, so two machines never compute the same session id, and a
    fixture that let them collide would make a cross-machine pull look like
    a round trip that worked.
    """
    sess, root, kid = f"{tag}-sess-1", f"{tag}-thr-root", f"{tag}-thr-kid"
    conn = db.connect(path)
    db.migrate(conn)
    # An upsert, not an insert: `cci login` claims this host and creates the
    # row, so a fixture seeding a database the CLI has already touched must
    # land on it rather than collide with it.
    conn.execute(
        """INSERT INTO host (host_id, hostname, os, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (host_id) DO UPDATE SET
               hostname = excluded.hostname, os = excluded.os,
               first_seen = excluded.first_seen, last_seen = excluded.last_seen""",
        (host_id, hostname, "darwin", 1_700_000_000_000, 1_700_000_900_000),
    )
    conn.execute(
        """INSERT INTO project_group
               (group_id, name, origin, match_key, remote_url, forge, owner, repo,
                web_url, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("grp-loam", "harbor-cli", "remote", REMOTE_URL, REMOTE_URL, "github", "alice",
         "harbor-cli", REMOTE_URL, 1_700_000_000_000, 1_700_000_000_000),
    )
    conn.execute(
        "INSERT INTO project (project_id, root_path, name, group_id, group_pinned) "
        "VALUES (?, ?, ?, ?, 0)",
        ("proj-loam", root_path, root_path.rsplit("/", 1)[-1], "grp-loam"),
    )
    conn.execute(
        """INSERT INTO project_probe
               (project_id, host_id, git_remote, git_common_dir, path_exists, detected_at)
           VALUES (?, ?, ?, ?, 1, ?)""",
        ("proj-loam", host_id, REMOTE_URL, root_path + "/.git", 1_700_000_000_000),
    )
    conn.execute(
        """INSERT INTO session
               (id, native_id, source, host_id, project_id, cwd, git_branch,
                cli_version, started_at, ended_at, event_count, active_ms)
           VALUES (?, ?, 'claude_code', ?, 'proj-loam', ?, 'main', '1.0', ?, ?, ?, ?)""",
        (sess, f"native-{tag}", host_id, root_path, 1_700_000_000_000,
         1_700_000_600_000, events, 600_000),
    )
    # A subagent thread whose parent is inserted first here, but which the
    # transfer may well page in the other order -- the ids sort the other way
    # round. A transport that does not put parents first fails the foreign
    # key rather than passing by luck.
    conn.execute(
        """INSERT INTO thread
               (id, native_id, session_id, parent_thread_id, is_subagent, agent_name,
                started_at, ended_at, event_count, active_ms)
           VALUES (?, ?, ?, NULL, 0, NULL, ?, ?, ?, ?)""",
        (root, f"nt-root-{tag}", sess, 1_700_000_000_000, 1_700_000_600_000,
         events, 600_000),
    )
    conn.execute(
        """INSERT INTO thread
               (id, native_id, session_id, parent_thread_id, is_subagent, agent_name,
                started_at, ended_at, event_count, active_ms)
           VALUES (?, ?, ?, ?, 1, 'ClientCodenameAgent', ?, ?, 1, 60000)""",
        (kid, f"nt-kid-{tag}", sess, root, 1_700_000_100_000, 1_700_000_160_000),
    )
    for i in range(events):
        conn.execute(
            """INSERT INTO event
                   (id, session_id, thread_id, native_event_id, ts, ordinal, kind,
                    model, tool_name, tool_use_id, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, cache_write_1h_tokens)
               VALUES (?, ?, ?, ?, ?, ?, 'assistant', 'sonnet',
                       'Read', ?, 10, 20, 30, 40, ?)""",
            (f"{tag}-ev-{i:06d}", sess, root, f"nev-{tag}-{i}",
             1_700_000_000_000 + i * 1000, i, f"tu-{tag}-{i}", 5 if i % 2 else None),
        )
    conn.execute(
        """INSERT INTO span (id, session_id, thread_id, started_at, ended_at,
                             event_count, attended)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        (f"{tag}-span-1", sess, root, 1_700_000_000_000, 1_700_000_600_000, events),
    )
    conn.execute(
        """INSERT INTO span (id, session_id, thread_id, started_at, ended_at,
                             event_count, attended)
           VALUES (?, ?, ?, ?, ?, 1, 0)""",
        (f"{tag}-span-2", sess, kid, 1_700_000_100_000, 1_700_000_160_000),
    )
    return conn


@pytest.fixture
def local(tmp_path):
    conn = _make_db(tmp_path / "local.db")
    yield conn
    conn.close()


def recorder():
    """A `sleep` that really sleeps and records what it was asked for.

    Faking the wait does not work against a live server and that is the
    point: the throttle in `device.poll` is measured on the server's own
    clock, so a client that does not actually wait gets `slow_down` forever
    and the scripted GitHub result is never reached. The harness runs the
    provider with a one-second interval to keep the honest version cheap.
    """
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        time.sleep(seconds)

    return waits, sleep


def sign_in(live, actor: str = ACTOR, repos=()) -> tuple[remote.Client, remote.Identity]:
    """Complete a real device flow and return an authenticated client."""
    anonymous = remote.Client(live.base_url)
    flow = anonymous.device_start(f"cci test on {actor}")
    token = f"gh-token-{actor}"
    live.register_identity(token, f"gh-sub-{actor}", actor, list(repos))
    live.script_device(live.latest_device(), [{"accessToken": token}])
    identity = anonymous.device_await(flow, sleep=lambda _s: None)
    return remote.Client(live.base_url, identity.token), identity


# --------------------------------------------------------------------------
# sign-in
# --------------------------------------------------------------------------


def test_healthz_needs_no_credential(live):
    """An unauthenticated probe must work, or `cci doctor` cannot tell
    "server down" from "token bad"."""
    body = remote.Client(live.base_url).healthz()
    assert body["status"] == "ok"
    assert body["migrations"], "a server with no migrations applied is not ready"


def test_device_start_prints_a_code_for_the_person_and_keeps_one_for_itself(live):
    """If the client showed the device code instead of the user code, the
    person would type a bearer-equivalent secret into a web page."""
    flow = remote.Client(live.base_url).device_start("cci test")
    assert flow.user_code and flow.device_code
    assert flow.user_code != flow.device_code
    assert flow.verification_uri.startswith("http")
    assert flow.user_code in flow.verification_uri_complete
    assert flow.interval >= 1
    assert flow.expires_at > 0


def test_polling_waits_out_authorization_pending(live):
    """A client that gave up on the first `authorization_pending` would make
    sign-in impossible: nobody approves a browser prompt in under a second."""
    client = remote.Client(live.base_url)
    flow = client.device_start("cci test")
    live.register_identity("gh-token-alice", "1", ACTOR)
    live.script_device(live.latest_device(), [
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"accessToken": "gh-token-alice"},
    ])

    waits, sleep = recorder()
    identity = client.device_await(flow, sleep=sleep)

    assert identity.actor == ACTOR
    assert identity.account_id.startswith("acc_")
    assert identity.token.startswith("ccis_")
    assert len(waits) == 2, "one wait per pending poll"
    assert all(w >= flow.interval for w in waits), (
        "sleeping less than the interval is what earns a slow_down"
    )


def test_slow_down_raises_the_interval_and_the_client_keeps_it_raised(live):
    """The server throttles an impatient CLI *before* forwarding to GitHub,
    because one client burning the instance's rate limit stops everyone
    signing in. A client that ignores the raise keeps earning it."""
    client = remote.Client(live.base_url)
    flow = client.device_start("cci test")
    live.register_identity("gh-token-alice", "1", ACTOR)
    live.script_device(live.latest_device(), [{"error": "authorization_pending"}])

    first = client.device_poll(flow.device_code)
    assert first.error == remote.PENDING

    # Immediately again: too fast by the server's own clock, no GitHub call.
    throttled = client.device_poll(flow.device_code)
    assert throttled.error == remote.SLOW_DOWN
    assert throttled.interval > flow.interval, "slow_down must carry a bigger number"

    # And the client adopts it rather than its own original guess. The first
    # poll of this loop is still inside the raised window, so the server
    # throttles once more; what matters is that the wait that follows is the
    # raised one and not `flow.interval`.
    live.script_device(live.latest_device(), [{"accessToken": "gh-token-alice"}])
    waits, sleep = recorder()
    identity = client.device_await(flow, sleep=sleep)

    assert identity.actor == ACTOR
    assert waits, "a throttled client must wait before polling again"
    assert min(waits) >= throttled.interval, (
        f"waited {waits} after being told the minimum is {throttled.interval}s"
    )


def test_access_denied_stops_rather_than_retrying(live):
    """Retrying a declined sign-in polls a dead flow forever and never tells
    the user their own click is the reason."""
    client = remote.Client(live.base_url)
    flow = client.device_start("cci test")
    live.script_device(live.latest_device(), [{"error": "access_denied"}])

    with pytest.raises(remote.DeviceFlowError) as caught:
        client.device_await(flow, sleep=lambda _s: pytest.fail("must not retry"))
    assert caught.value.code == remote.DENIED
    assert "declined" in str(caught.value)


def test_expired_token_stops_and_says_to_start_again(live):
    """`expired_token` is terminal. A client that waits on it hangs until
    somebody notices, with no output saying what to do."""
    client = remote.Client(live.base_url)
    flow = client.device_start("cci test")
    live.script_device(live.latest_device(), [{"error": "expired_token"}])

    with pytest.raises(remote.DeviceFlowError) as caught:
        client.device_await(flow, sleep=lambda _s: pytest.fail("must not retry"))
    assert caught.value.code == remote.EXPIRED
    assert "cci login" in str(caught.value)


def test_an_unknown_device_code_stops(live):
    """An already-exchanged or invented code must not be treated as pending,
    or a replayed command polls forever against nothing."""
    client = remote.Client(live.base_url)
    with pytest.raises(remote.DeviceFlowError) as caught:
        client.device_await(
            remote.DeviceFlow("dev_nope", "X", "http://x", "http://x",
                              9_999_999_999_999, 5),
            sleep=lambda _s: pytest.fail("must not retry"),
        )
    assert caught.value.code == remote.INVALID_DEVICE_CODE


def test_a_device_code_cannot_be_exchanged_twice(live):
    """Replay -- from a shell history, a log, a retried request -- must not
    mint a second token for a code somebody else may now be holding."""
    anonymous = remote.Client(live.base_url)
    flow = anonymous.device_start("cci test")
    live.register_identity("gh-token-alice", "1", ACTOR)
    live.script_device(live.latest_device(), [{"accessToken": "gh-token-alice"}])

    first = anonymous.device_await(flow, sleep=lambda _s: None)
    assert first.token

    replayed = anonymous.device_poll(flow.device_code)
    assert replayed.identity is None
    assert replayed.error == remote.INVALID_DEVICE_CODE


def test_whoami_reports_the_identity_as_an_array(live):
    """`identities` is a list today so that email sign-in needs no client
    change; a client that reads `[0]` only would break on that day."""
    client, identity = sign_in(live)
    who = client.whoami()
    assert who["accountId"] == identity.account_id
    assert who["actor"] == ACTOR
    assert isinstance(who["identities"], list) and who["identities"]
    assert who["identities"][0]["provider"] == "github"
    assert who["token"]["name"] == f"cci test on {ACTOR}"


def test_logout_revokes_the_token_that_presented_it(live):
    """A logout that leaves the token live means a lost laptop keeps pushing."""
    client, _ = sign_in(live)
    client.logout()
    with pytest.raises(remote.AuthRequired):
        client.whoami()


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_a_401_names_the_command_that_fixes_it(live):
    """The whole point of the error layer: a rejected token must print a
    sentence and a command, not a urllib traceback."""
    client = remote.Client(live.base_url, "ccis_not-a-real-token")
    with pytest.raises(remote.AuthRequired) as caught:
        client.personal_status()
    message = str(caught.value)
    assert "cci login" in message
    assert "Traceback" not in message
    assert "HTTPError" not in message


def test_no_token_at_all_fails_before_the_socket(live):
    """Asking the server whether we are signed in is a slower way to print
    the same sentence, and it fails differently when the server is down."""
    with pytest.raises(remote.AuthRequired) as caught:
        remote.Client(live.base_url).personal_status()
    assert "cci login" in str(caught.value)


def test_an_unreachable_server_says_capture_is_unaffected():
    """The one thing a user must not conclude from a network error is that
    their local history stopped being recorded."""
    client = remote.Client("http://127.0.0.1:1", "ccis_x", timeout_s=2)
    with pytest.raises(remote.Unreachable) as caught:
        client.personal_status()
    assert "local capture is unaffected" in str(caught.value)


def test_pushing_an_unknown_column_is_refused_not_dropped(live, local):
    """A silently dropped value is the failure mode the contract refuses on
    its own side; the client must see the refusal rather than assume success."""
    client, _ = sign_in(live)
    with pytest.raises(remote.RemoteError) as caught:
        client.personal_push(HOST_ID, "host",
                             ["host_id", "hostname", "os", "first_seen",
                              "last_seen", "not_a_column"],
                             [[HOST_ID, "studio", "darwin", 1, 2, "x"]])
    assert caught.value.code in {"unknown_column", "missing_column"}


def test_an_excluded_table_says_why_it_is_excluded(live):
    """`sync.EXCLUDED` carries a reason per table and the server returns it.
    A bare 400 would send somebody looking for a bug that is a decision."""
    client, _ = sign_in(live)
    with pytest.raises(remote.RemoteError) as caught:
        client.personal_push(HOST_ID, "ingest_file", ["host_id"], [[HOST_ID]])
    assert caught.value.code == "table_not_transferred"
    assert "local path" in str(caught.value)


# --------------------------------------------------------------------------
# the personal store
# --------------------------------------------------------------------------


def test_the_client_does_not_send_host_account_id(live):
    """`sync.TABLES` carries `host.account_id` and the personal store does
    not model it -- the account comes from the token, and every key is
    already composite on it. Sending it is a 400, so the plan must drop
    exactly that column and no other."""
    client, _ = sign_in(live)
    plans = {p.name: p for p in remote.plan_tables(client.personal_tables())}
    assert plans["host"].omitted == ("account_id",)
    assert "account_id" not in plans["host"].columns
    assert all(p.omitted == () for n, p in plans.items() if n != "host"), (
        "a column dropped without an entry in WIRE_OMITTED is a silent data loss"
    )


def test_push_moves_every_table_and_pull_brings_it_all_back(live, local, home, tmp_path):
    """A round trip that loses a table is the failure `test_sync` exists for,
    arriving over the other transport."""
    client, _ = sign_in(live)
    pushed = remote.push(local, client, HOST_ID, config_dir=home)

    assert pushed.rows["event"] == 5
    assert pushed.rows["thread"] == 2
    assert set(pushed.rows) == {t.name for t in sync.TABLES}

    status = client.personal_status()
    assert status["totalRows"] == sum(pushed.rows.values())
    assert [h["hostId"] for h in status["hosts"]] == [HOST_ID]

    other = db.connect(tmp_path / "second-machine.db")
    db.migrate(other)
    try:
        pulled = remote.pull(other, client, config_dir=tmp_path / "other-home")
        assert pulled.rows == pushed.rows
        # The second machine can now answer the question the first one could.
        assert other.execute("SELECT count(*) FROM event").fetchone()[0] == 5
        assert other.execute(
            "SELECT cwd FROM session WHERE id = 'a-sess-1'"
        ).fetchone()[0] == ROOT_PATH, "the personal store is where paths DO travel"
        assert other.execute(
            "SELECT parent_thread_id FROM thread WHERE id = 'a-thr-kid'"
        ).fetchone()[0] == "a-thr-root"
    finally:
        other.close()


def test_re_pushing_unchanged_data_sends_nothing(live, local, home):
    """Every id is a content hash, so a second push cannot change anything.
    Spending 95 requests to prove that every 15 minutes is what makes a
    background job a problem rather than a feature."""
    client, _ = sign_in(live)
    first = remote.push(local, client, HOST_ID, config_dir=home)
    assert first.requests > 0

    second = remote.push(local, client, HOST_ID, config_dir=home)
    assert second.requests == 0
    assert second.total == 0
    assert sorted(second.skipped) == sorted(t.name for t in sync.TABLES)

    # And the store is unchanged, not emptied.
    assert client.personal_status()["totalRows"] == first.total


def test_a_changed_row_is_noticed_even_when_the_count_is_the_same(live, local, home):
    """`cci backfill` fills `event.cache_write_1h_tokens` in place: no new id,
    no new row, no later timestamp. A digest over counts or max(ts) would skip
    the table and leave the server pricing 41% of cache writes wrong forever."""
    client, _ = sign_in(live)
    remote.push(local, client, HOST_ID, config_dir=home)

    local.execute("UPDATE event SET cache_write_1h_tokens = 99 WHERE id = 'a-ev-000000'")
    again = remote.push(local, client, HOST_ID, config_dir=home)

    assert "event" not in again.skipped
    assert again.rows.get("event") == 5


def test_an_interrupted_push_resumes_instead_of_restarting(live, local, home):
    """191,475 rows over a flaky connection: a client that starts from zero on
    every blip never finishes. The resume point must survive the failure."""
    client, _ = sign_in(live)
    total_rows = 5

    class Flaky(remote.Client):
        """Dies part-way through `event`, after at least one batch landed."""

        def __init__(self, inner, fail_on):
            super().__init__(inner.base_url, inner.token)
            self.event_pushes = 0
            self.fail_on = fail_on

        def personal_push(self, host_id, table, columns, rows):
            if table == "event":
                self.event_pushes += 1
                if self.event_pushes > self.fail_on:
                    raise remote.Unreachable("the network went away mid-push")
            return super().personal_push(host_id, table, columns, rows)

    flaky = Flaky(client, fail_on=1)
    with pytest.raises(remote.Unreachable):
        remote.push(local, flaky, HOST_ID, config_dir=home, batch=2)
    landed = client.personal_status()
    events_landed = dict((t["table"], t["rows"]) for t in landed["tables"])["event"]
    assert 0 < events_landed < total_rows, "the test needs a genuinely partial push"

    resumed = remote.push(local, client, HOST_ID, config_dir=home, batch=2)
    assert resumed.resumed.get("event") == events_landed, (
        "the second run must pick up where the first stopped"
    )
    assert resumed.rows["event"] == total_rows - events_landed, (
        "and must not re-send the rows that already landed"
    )
    after = dict((t["table"], t["rows"]) for t in client.personal_status()["tables"])
    assert after["event"] == total_rows


def test_a_table_that_changed_under_an_abandoned_push_starts_over(live, local, home):
    """A bare cursor cannot tell "we stopped here" from "we stopped here, and
    then the rows moved". Resuming the second case skips whatever shifted
    behind the cursor, and nothing ever reports it."""
    client, _ = sign_in(live)

    class Flaky(remote.Client):
        def personal_push(self, host_id, table, columns, rows):
            if table == "event":
                raise remote.Unreachable("dropped")
            return super().personal_push(host_id, table, columns, rows)

    with pytest.raises(remote.Unreachable):
        remote.push(local, Flaky(client.base_url, client.token), HOST_ID,
                    config_dir=home, batch=2)

    local.execute(
        """INSERT INTO event
               (id, session_id, thread_id, native_event_id, ts, ordinal, kind,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
           VALUES ('a-ev-0000005', 'a-sess-1', 'a-thr-root', 'nev-extra', 1, 99, 'user',
                   0, 0, 0, 0)"""
    )
    again = remote.push(local, client, HOST_ID, config_dir=home, batch=2)
    assert "event" not in again.resumed, "a changed table must not resume"
    assert again.rows["event"] == 6


def test_the_batch_cap_is_refused_before_the_request(live):
    """413 is the server's answer; provoking it from a client that knows the
    cap is a bug the user pays a round trip for."""
    with pytest.raises(ValueError, match="cap"):
        remote.push(None, remote.Client(live.base_url, "x"), HOST_ID,
                    config_dir=".", batch=remote.MAX_BATCH_ROWS + 1)


def test_pull_is_paginated_and_resumable(live, local, home, tmp_path):
    """The cursor is opaque and must be handed back verbatim. A client that
    parses it, or that pages by offset, re-scans 190,000 rows to discard them."""
    client, _ = sign_in(live)
    remote.push(local, client, HOST_ID, config_dir=home)

    page = client.personal_pull("event", limit=2)
    assert len(page["rows"]) == 2
    assert page["nextCursor"], "more rows exist, so there must be a cursor"
    assert page["limit"] == 2

    second = client.personal_pull("event", limit=2, cursor=page["nextCursor"])
    assert len(second["rows"]) == 2
    first_ids = {r[page["columns"].index("id")] for r in page["rows"]}
    second_ids = {r[second["columns"].index("id")] for r in second["rows"]}
    assert not (first_ids & second_ids), "a keyset page must not repeat a row"


def test_pull_sees_the_other_machine(live, local, home, tmp_path):
    """The product ask: sign in on a second machine and it just works."""
    client, _ = sign_in(live)
    remote.push(local, client, HOST_ID, config_dir=home)

    laptop = _make_db(tmp_path / "laptop.db", host_id=LAPTOP_ID,
                      hostname="laptop", tag="b")
    try:
        pulled = remote.pull(laptop, client, config_dir=tmp_path / "laptop-home")
        assert pulled.rows["host"] == 1, "the studio's host row must arrive"
        assert {r[0] for r in laptop.execute("SELECT host_id FROM host")} == {
            HOST_ID, LAPTOP_ID
        }, "both machines are now visible from the laptop"
        # And the studio's work is readable here, which is the product ask.
        assert laptop.execute(
            "SELECT count(*) FROM event WHERE session_id = 'a-sess-1'"
        ).fetchone()[0] == 5
    finally:
        laptop.close()


# --------------------------------------------------------------------------
# the team store
# --------------------------------------------------------------------------


def test_publish_sends_the_projection_and_the_withheld_counters(live, local):
    """Rule 3: withheld time stays counted. A team view that silently drops
    part of somebody's week is not private, it is wrong."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)
    assert pub.repos and pub.sessions and pub.spans

    results = remote.publish(local, client, pub, host_id=HOST_ID)
    assert results["repos"].applied == len(pub.repos)
    assert results["sessions"].applied == len(pub.sessions)
    assert results["spans"].applied == len(pub.spans)
    assert results["withheld"].applied == 1

    summary = client.team_summary()
    assert summary["sessions"] == len(pub.sessions)
    assert summary["spans"] == len(pub.spans)
    assert summary["withheld"]["rangeFiltered"] is False, (
        "withheld is a corpus total and the renderer has to be told so"
    )
    mine = [a for a in summary["withheld"]["byActor"] if a["actor"] == ACTOR]
    assert mine, "the caller's own withheld figure is always present"


def test_publish_never_sends_a_path_or_a_path_derived_id(live, local):
    """The release blocker. `project_id` IS sha256(root_path), so shipping one
    ships the other to anyone who can guess the path -- and 555 guesses
    recovered 20% of the author's corpus."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)

    sent: list[dict] = []

    class Recording(remote.Client):
        def team_publish(self, kind, actor, rows):
            sent.extend(rows)
            return super().team_publish(kind, actor, rows)

    remote.publish(local, Recording(client.base_url, client.token), pub, host_id=HOST_ID)

    blob = repr(sent)
    for forbidden in (ROOT_PATH, "proj-loam", "/Users/alice", HOSTNAME,
                      "ClientCodenameAgent", "SecretClientProject",
                      "native-a", "nt-root-a"):
        assert forbidden not in blob, f"{forbidden!r} crossed the boundary"

    # And the store itself cannot hand one back, because it has no column for it.
    for session in client.team_sessions()["sessions"]:
        assert set(session) == {
            "sessionId", "repoId", "actor", "source", "gitBranch",
            "startedAt", "endedAt", "activeMs", "eventCount",
        }


def test_the_withheld_row_carries_a_host_id_and_nobody_can_read_it_back(live, local):
    """The one host-derived value the team store receives, and the contract
    requires it: §4.1's `withheld` row is keyed on `hostId`, or two machines'
    counters overwrite each other.

    `redact.FIELDS` classifies `host.host_id` DERIVED -- "it silently links
    one person's machines" -- and `redact.Publication` has no field for it, so
    the client supplies it out of band. That is safe only because it is an
    opaque `uuid4()` and because no read endpoint ever returns it. Both halves
    are asserted here; if a future endpoint starts serving it, this fails."""
    alice, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)
    remote.publish(local, alice, pub, host_id=HOST_ID)

    rendered = repr(alice.team_summary()) + repr(alice.team_actors()) + repr(
        alice.team_sessions()) + repr(alice.team_repos())
    assert HOST_ID not in rendered, (
        "the host id is a key for the counters, never something to serve back"
    )


def test_publish_refuses_a_projection_that_leaks_and_sends_nothing(live, tmp_path):
    """Enforced in the transport, not only in the report. A refusal a caller
    can skip by not calling the reporting function is not a refusal."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    conn = _make_db(tmp_path / "leaky.db")
    try:
        pub = redact.publication(conn, ACTOR, host_id=HOST_ID)
        # A branch name carrying the local path is exactly the shape
        # `redact.audit` proves cannot be published.
        pub.sessions[0] = type(pub.sessions[0])(
            **{**vars(pub.sessions[0]), "git_branch": f"wip{ROOT_PATH}"}
        )

        with pytest.raises(remote.Unsafe) as caught:
            remote.publish(conn, client, pub, host_id=HOST_ID)
        assert "audit" in str(caught.value)
        assert client.team_summary()["sessions"] == 0, "nothing may have been sent"
    finally:
        conn.close()


def test_publish_refuses_while_a_column_is_unclassified(live, tmp_path):
    """Closed by default is only real if the publisher refuses to run while a
    migration has added a column nobody has ruled on."""
    client, _ = sign_in(live)
    conn = _make_db(tmp_path / "unclassified.db")
    try:
        conn.execute("ALTER TABLE session ADD COLUMN mystery TEXT")
        pub = redact.publication(conn, ACTOR, host_id=HOST_ID)
        with pytest.raises(remote.Unsafe) as caught:
            remote.publish(conn, client, pub, host_id=HOST_ID)
        assert "session.mystery" in str(caught.value)
        assert "cci privacy" in str(caught.value)
    finally:
        conn.close()


def test_publishing_under_somebody_elses_actor_is_refused(live, local):
    """`actor` is checked, not trusted. Overwriting it server-side would be
    worse: `cci privacy` would stop describing what was actually sent."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, "mallory", host_id=HOST_ID)
    with pytest.raises(remote.Forbidden) as caught:
        remote.publish(local, client, pub, host_id=HOST_ID)
    assert caught.value.code == "actor_mismatch"


def test_republishing_is_idempotent(live, local):
    """Same reason as the personal push: the ids are content hashes, so a
    second publish must report the same numbers and create no duplicates."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)
    first = remote.publish(local, client, pub, host_id=HOST_ID)
    before = client.team_summary()

    second = remote.publish(local, client, pub, host_id=HOST_ID)
    assert {k: v.applied for k, v in second.items()} == {
        k: v.applied for k, v in first.items()
    }
    after = client.team_summary()
    assert after["sessions"] == before["sessions"]
    assert after["spans"] == before["spans"]
    assert after["activeMs"] == before["activeMs"]


def test_a_repo_outside_the_scope_is_simply_absent(live, local):
    """§4.2: there is no endpoint that takes a repoId and confirms it exists.
    A filter on an unknown id is ignored, never an error."""
    client, _ = sign_in(live, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)
    remote.publish(local, client, pub, host_id=HOST_ID)

    scoped = client.team_summary(repo=["repo_does_not_exist"])
    assert scoped["sessions"] == 0
    assert scoped["scope"]["repos"] >= 1, "the caller's own scope is unaffected"


# --------------------------------------------------------------------------
# the commands themselves, against the same live server
# --------------------------------------------------------------------------


def _login_via_cli(live, home, actor: str = ACTOR, repos=("alice/harbor-cli",)) -> int:
    """Run `cci login` for real, approving the flow from another thread.

    The command blocks on its own poll loop, so the approval has to arrive
    while it is running -- which is exactly the shape of the real thing, and
    the reason this is worth testing through the command rather than through
    `remote` alone.
    """
    import threading

    from cc_insights import cli

    before = set()
    try:
        before = set(live._get("/test/provider/devices")["devices"])
    except Exception:
        pass

    def approve() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                now = set(live._get("/test/provider/devices")["devices"])
            except Exception:
                now = set()
            new = now - before
            if new:
                token = f"gh-token-{actor}"
                live.register_identity(token, f"gh-sub-{actor}", actor, list(repos))
                live.script_device(sorted(new)[0], [{"accessToken": token}])
                return
            time.sleep(0.1)

    helper = threading.Thread(target=approve, daemon=True)
    helper.start()
    try:
        return cli.main(["--config-dir", str(home), "login",
                         "--server", live.base_url])
    finally:
        helper.join(timeout=5)


def test_cci_login_then_sync_push_then_team_works_end_to_end(live, home, tmp_path, capsys):
    """The product ask, through the commands a person actually types."""
    from cc_insights import account, cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    capsys.readouterr()

    assert _login_via_cli(live, home) == 0
    out = capsys.readouterr().out
    assert f"signed in as {ACTOR}" in out

    stored = account.load(home)
    assert stored is not None and stored.server_url == live.base_url

    # Put something in the database this machine can push.
    cfg = config_mod.load(home, create=False)
    seeded = _make_db(cfg.db_path, host_id=cfg.host_id, tag="c")
    seeded.execute("UPDATE host SET account_id = ? WHERE host_id = ?",
                   (stored.account_id, cfg.host_id))
    seeded.close()

    assert cli.main(["--config-dir", str(home), "sync", "push"]) == 0
    pushed = capsys.readouterr().out
    assert "your account at" in pushed, "the command must say which backend it used"

    # A second push is free.
    assert cli.main(["--config-dir", str(home), "sync", "push"]) == 0
    assert "already up to date" in capsys.readouterr().out

    assert cli.main(["--config-dir", str(home), "sync", "status"]) == 0
    assert "← this machine" in capsys.readouterr().out


def test_cci_publish_refuses_without_confirmation_on_first_use(live, home, capsys,
                                                               monkeypatch):
    """The one command that sends data somewhere other people read. It must
    not be the kind of command you can run by accident."""
    from cc_insights import cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="d").close()
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _p="": "no thanks")
    assert cli.main(["--config-dir", str(home), "publish"]) == 1
    out = capsys.readouterr().out
    assert "WITHHELD" in out and "SENDING" in out
    assert "nothing was sent" in out

    client = remote.Client(live.base_url, __import__(
        "cc_insights.account", fromlist=["account"]).load(home).token)
    assert client.team_summary()["sessions"] == 0


def test_cci_publish_reports_before_it_sends_and_then_sends(live, home, capsys,
                                                            monkeypatch):
    """The withholding report is a decision, not a receipt: it has to be on
    screen before the first request, not after the last one."""
    from cc_insights import account, cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="e").close()
    capsys.readouterr()

    monkeypatch.setattr("builtins.input", lambda _p="": "publish")
    assert cli.main(["--config-dir", str(home), "publish"]) == 0
    out = capsys.readouterr().out
    assert out.index("WITHHELD") < out.index("published "), (
        "the report must precede the send"
    )

    # Confirmed once: the second run must not ask again.
    monkeypatch.setattr("builtins.input",
                        lambda _p="": pytest.fail("must only confirm once"))
    assert cli.main(["--config-dir", str(home), "publish"]) == 0
    capsys.readouterr()

    assert cli.main(["--config-dir", str(home), "team"]) == 0
    team = capsys.readouterr().out
    assert "REPOS IN SCOPE" in team and "harbor-cli" in team
    assert ROOT_PATH not in team

    token = account.load(home).token
    assert remote.Client(live.base_url, token).team_summary()["sessions"] > 0


def test_cci_publish_dry_run_sends_nothing(live, home, capsys):
    """A way to read the report without a decision attached to it."""
    from cc_insights import account, cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="f").close()
    capsys.readouterr()

    assert cli.main(["--config-dir", str(home), "publish", "--dry-run"]) == 0
    assert "nothing was sent" in capsys.readouterr().out
    token = account.load(home).token
    assert remote.Client(live.base_url, token).team_summary()["sessions"] == 0


def test_the_background_job_step_pushes_when_signed_in(live, home, capsys):
    """`cci sync auto` is the whole of deliverable 3: a second machine stays
    current without being told, and does nothing at all when signed out."""
    from cc_insights import account, cli, config as config_mod

    assert cli.main(["--config-dir", str(home), "init"]) == 0
    capsys.readouterr()
    # Signed out: a silent no-op, exit 0.
    assert cli.main(["--config-dir", str(home), "sync", "auto"]) == 0
    assert capsys.readouterr().out == ""

    assert _login_via_cli(live, home) == 0
    cfg = config_mod.load(home, create=False)
    _make_db(cfg.db_path, host_id=cfg.host_id, tag="g").close()
    capsys.readouterr()

    assert cli.main(["--config-dir", str(home), "sync", "auto", "--verbose"]) == 0
    assert "auto-push" in capsys.readouterr().out

    token = account.load(home).token
    assert remote.Client(live.base_url, token).personal_status()["totalRows"] > 0


def test_team_reads_are_scoped_per_caller(live, local, tmp_path):
    """The load-bearing rule: an aggregate computed outside the viewer's scope
    leaks the existence of a repo they cannot see the moment they read a total."""
    alice, _ = sign_in(live, actor=ACTOR, repos=["alice/harbor-cli"])
    pub = redact.publication(local, ACTOR, host_id=HOST_ID)
    remote.publish(local, alice, pub, host_id=HOST_ID)

    bob, _ = sign_in(live, actor="bob", repos=["bob/Unrelated"])
    assert bob.team_repos() == [] or all(
        r["repoId"] != pub.repos[0].repo_id for r in bob.team_repos()
    )
    assert bob.team_summary()["sessions"] == 0
    assert bob.team_sessions()["sessions"] == []
