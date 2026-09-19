"""Tests for the local HTTP server.

These speak HTTP over a real socket rather than calling the handler directly.
The things most likely to break -- a status code, a header, a verb that should
never have been accepted -- only exist on the wire, and a handler exercised
in-process would happily "pass" while the server refused to start.

The database here is the same synthetic corpus `test_metrics.py` builds, so a
number asserted below can be checked by eye against that file.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from cc_insights import cli, derive, metrics, serve
from cc_insights.config import Config
from test_metrics import ALPHA_MS, SUB_MS, TOTAL_MS, _insert

TIMEOUT = 15
# `shutdown()` only takes effect at the next poll, so the stdlib default of
# half a second would be paid by every test in this file at teardown.
POLL = 0.02


@pytest.fixture
def cfg(conn, tmp_path: Path) -> Config:
    """The synthetic corpus, on disk, with a config pointing at it."""
    _insert(conn)
    derive.derive(conn, idle_threshold_s=300)
    conn.commit()
    return Config(host_id="h1", hostname="test-host", db_path=tmp_path / "test.db",
                  idle_threshold_s=300, config_dir=tmp_path)


@pytest.fixture
def server(cfg: Config):
    """A server on an ephemeral port, with no frontend build to serve."""
    srv = serve.make_server(cfg, port=0, dist_dir=None, quiet=True)
    thread = threading.Thread(target=srv.serve_forever, args=(POLL,), daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=TIMEOUT)
        assert not thread.is_alive(), "server thread did not stop"


def request(srv, path: str, *, method: str = "GET", headers: dict | None = None,
            data: bytes | None = None):
    """Returns `(status, headers, body_bytes)` -- errors included, not raised."""
    req = urllib.request.Request(srv.url.rstrip("/") + path, method=method, data=data,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.headers, exc.read()


def get_json(srv, path: str):
    status, headers, body = request(srv, path)
    assert headers.get("Content-Type") == "application/json; charset=utf-8"
    return status, json.loads(body)


# --------------------------------------------------------------------------
# the eight endpoints
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", metrics.ENDPOINTS)
def test_every_endpoint_answers_over_http(server, name):
    status, payload = get_json(server, f"/api/{name}")
    assert status == 200
    assert isinstance(payload, dict)
    # The same function, called directly, is what the response body must be.
    expected = metrics.endpoint(name, server.conn(), metrics.Filters(), server.cfg)
    if name == "meta":
        payload.pop("generatedAt"), expected.pop("generatedAt")
    assert payload == expected


def test_top_level_keys_match_the_contract(server):
    for name, keys in (
        ("meta", {"hostname", "firstTs", "lastTs", "sources", "projects", "agents",
                  "models", "idleThresholdS", "generatedAt"}),
        ("summary", {"sessions", "threads", "events", "spans", "activeMs", "bySource",
                     "humanInitiatedMs", "autonomousMs", "unattendedRootMs", "tokens"}),
        ("timeline", {"spans", "truncated", "limit"}),
        ("daily", {"days"}),
        ("concurrency", {"timeAtLevel", "peak", "peakAt", "wallMs", "activeMs",
                         "multiplier"}),
        ("projects", {"projects"}),
        ("agents", {"agents"}),
        ("heatmap", {"cells"}),
    ):
        status, payload = get_json(server, f"/api/{name}")
        assert status == 200 and set(payload) == keys, name


def test_filters_travel_through_the_query_string(server):
    _, everything = get_json(server, "/api/summary")
    _, alpha = get_json(server, "/api/summary?project=p-alpha")
    assert alpha["activeMs"] == ALPHA_MS + SUB_MS < everything["activeMs"]

    _, both = get_json(server, "/api/summary?project=p-alpha&project=p-beta")
    assert both["activeMs"] == TOTAL_MS

    _, root = get_json(server, "/api/summary?role=root")
    _, sub = get_json(server, "/api/summary?role=subagent")
    assert root["activeMs"] + sub["activeMs"] == everything["activeMs"]

    _, codex = get_json(server, "/api/summary?source=codex")
    assert codex["bySource"] == [{"source": "codex", "activeMs": codex["activeMs"]}]

    _, early = get_json(server, "/api/timeline?to=0")
    assert early["spans"] == []


def test_meta_ignores_filters(server):
    _, unfiltered = get_json(server, "/api/meta")
    _, filtered = get_json(server, "/api/meta?project=p-alpha&role=subagent")
    unfiltered.pop("generatedAt"), filtered.pop("generatedAt")
    assert unfiltered == filtered


# --------------------------------------------------------------------------
# errors: never a 200 with an error body
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    "/api/summary?from=yesterday",
    "/api/summary?to=12.5",
    "/api/timeline?role=human",
    "/api/daily?from=abc&project=p-alpha",
])
def test_a_malformed_filter_is_400_json(server, path):
    status, payload = get_json(server, path)
    assert status == 400
    assert isinstance(payload.get("error"), str) and payload["error"]


@pytest.mark.parametrize("path", ["/api/bogus", "/api/", "/api", "/nope", "/index.html",
                                  "/api/summary/extra"])
def test_an_unknown_path_is_404_json(server, path):
    status, payload = get_json(server, path)
    assert status == 404
    assert isinstance(payload.get("error"), str) and payload["error"]


def test_no_endpoint_ever_answers_200_with_an_error_body(server):
    for path in ("/api/meta", "/api/summary?project=p-alpha", "/api/bogus",
                 "/api/summary?from=nope"):
        status, payload = get_json(server, path)
        assert (status == 200) is ("error" not in payload), path


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "TRACE", "PROPFIND"])
def test_a_write_verb_is_405(server, method):
    data = b"{}" if method in ("POST", "PUT", "PATCH") else None
    status, headers, body = request(server, "/api/summary", method=method, data=data)
    assert status == 405
    assert headers.get("Allow") == "GET, HEAD, OPTIONS"
    assert headers.get("Content-Type") == "application/json; charset=utf-8"
    assert "error" in json.loads(body)


def test_a_write_verb_on_an_unknown_path_is_also_405(server):
    # The refusal comes before routing: nothing but a read ever runs.
    assert request(server, "/whatever", method="DELETE")[0] == 405


def test_a_broken_query_does_not_take_the_server_down(server):
    request(server, "/api/summary?from=nope")
    assert get_json(server, "/api/summary")[0] == 200


def test_a_query_that_blows_up_is_500_json_and_the_server_survives(server, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(metrics, "endpoint", boom)
    status, payload = get_json(server, "/api/summary")
    assert status == 500 and "the database went away" in payload["error"]

    monkeypatch.undo()
    assert get_json(server, "/api/summary")[0] == 200


# --------------------------------------------------------------------------
# HTTP mechanics
# --------------------------------------------------------------------------
def test_head_returns_the_headers_and_no_body(server):
    status, headers, body = request(server, "/api/meta", method="HEAD")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0
    assert headers["Content-Type"] == "application/json; charset=utf-8"


def test_options_is_a_preflight_answer(server):
    status, headers, _ = request(server, "/api/summary", method="OPTIONS")
    assert status == 204
    assert headers.get("Allow") == "GET, HEAD, OPTIONS"
    assert headers.get("Access-Control-Allow-Methods") == "GET, HEAD, OPTIONS"


def test_cors_allows_the_vite_dev_server(server):
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        _, headers, _ = request(server, "/api/meta", headers={"Origin": origin})
        assert headers.get("Access-Control-Allow-Origin") == origin
        assert headers.get("Vary") == "Origin"


def test_cors_does_not_hand_its_data_to_any_other_origin(server):
    _, headers, _ = request(server, "/api/meta", headers={"Origin": "https://evil.example"})
    assert headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"


def test_it_listens_on_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_content_length_is_correct_on_every_response(server):
    for path in ("/api/timeline", "/api/bogus", "/"):
        _, headers, body = request(server, path)
        assert int(headers["Content-Length"]) == len(body), path


# --------------------------------------------------------------------------
# read-only, twice over
# --------------------------------------------------------------------------
def test_the_servers_connection_cannot_write(server):
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        server.conn().execute("DELETE FROM span")
    assert server.conn().execute("PRAGMA query_only").fetchone()[0] == 1
    assert get_json(server, "/api/summary")[1]["spans"] == 4


def test_each_thread_gets_its_own_connection(server):
    seen: list[int] = []

    def grab():
        seen.append(id(server.conn()))

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT)
    assert len(set(seen)) == 3
    assert id(server.conn()) == id(server.conn())   # ... and reused within one


def test_concurrent_requests_all_succeed(server):
    results: list[int] = []

    def hit(name):
        results.append(get_json(server, f"/api/{name}")[0])

    threads = [threading.Thread(target=hit, args=(n,)) for n in metrics.ENDPOINTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT)
    assert results == [200] * len(metrics.ENDPOINTS)


# --------------------------------------------------------------------------
# static files
# --------------------------------------------------------------------------
def test_root_explains_itself_when_the_frontend_is_not_built(server):
    status, headers, body = request(server, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    text = body.decode()
    assert "npm run build" in text and "/api/summary" in text


def test_it_serves_a_built_frontend(cfg, tmp_path):
    """`frontend/dist` is owned by the frontend build, so this test points the
    server at its own directory rather than writing into the real one."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>dash</title>")
    (dist / "assets" / "app.js").write_text("export const x = 1;\n")

    srv = serve.make_server(cfg, port=0, dist_dir=dist, quiet=True)
    thread = threading.Thread(target=srv.serve_forever, args=(POLL,), daemon=True)
    thread.start()
    try:
        status, headers, body = request(srv, "/")
        assert status == 200 and b"dash" in body
        assert headers["Content-Type"] == "text/html; charset=utf-8"

        status, headers, body = request(srv, "/assets/app.js")
        assert status == 200 and b"export const x" in body
        assert headers["Content-Type"].startswith("text/javascript")

        # The API still wins over any file that might share its path.
        assert get_json(srv, "/api/summary")[0] == 200
        # A missing asset is a 404, not the index page: a typo must be visible.
        assert get_json(srv, "/assets/missing.js")[0] == 404
        # And nothing outside the build directory is reachable.
        for path in ("/../config.toml", "/assets/../../test.db", "/%2e%2e/test.db"):
            assert request(srv, path)[0] == 404, path
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=TIMEOUT)


# --------------------------------------------------------------------------
# `cci serve`
# --------------------------------------------------------------------------
def test_serve_is_wired_into_the_cli():
    args = cli.build_parser().parse_args(["serve", "--port", "9999", "--no-open"])
    assert args.fn is cli.cmd_serve and args.port == 9999 and args.no_open is True

    default = cli.build_parser().parse_args(["serve"])
    assert default.port == serve.DEFAULT_PORT and default.no_open is False


def test_serve_without_a_database_exits_with_the_usual_message(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--config-dir", str(tmp_path), "serve"])
    assert exc.value.code == 1
    assert "cci init" in capsys.readouterr().err


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_cci_serve_answers_every_endpoint_and_shuts_down_cleanly(
        cfg, monkeypatch, capsys, tmp_path):
    """The whole command, end to end: `cci serve --port N --no-open`.

    The server object is captured as it is built so the test can stop it the
    way Ctrl-C would; everything else is the real path, including the port
    argument and the browser it must not open.
    """
    cfg.save()
    port = _free_port()
    made: list[serve.InsightsServer] = []
    real_make = serve.make_server

    def capture(*a, **kw):
        srv = real_make(*a, **kw)
        srv.quiet = True
        made.append(srv)
        return srv

    monkeypatch.setattr(serve, "make_server", capture)
    monkeypatch.setattr(serve.webbrowser, "open",
                        lambda *a, **kw: pytest.fail("--no-open must not open a browser"))

    rc: list[int] = []
    thread = threading.Thread(
        target=lambda: rc.append(
            cli.main(["--config-dir", str(cfg.config_dir), "serve",
                      "--port", str(port), "--no-open"])),
        daemon=True)
    thread.start()

    deadline = time.monotonic() + TIMEOUT
    while not made and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert made, "serve.make_server was never called"
    srv = made[0]
    assert srv.server_address[1] == port, "--port was not honoured"

    try:
        for name in metrics.ENDPOINTS:
            status, payload = get_json(srv, f"/api/{name}")
            assert status == 200 and isinstance(payload, dict), name
        assert get_json(srv, "/api/summary")[1]["activeMs"] == TOTAL_MS
        assert get_json(srv, "/api/nope")[0] == 404
        assert request(srv, "/api/summary", method="POST", data=b"{}")[0] == 405
    finally:
        srv.shutdown()                      # what Ctrl-C does to serve_forever
        thread.join(timeout=TIMEOUT)

    assert not thread.is_alive(), "`cci serve` did not return after shutdown"
    assert rc == [0]
    out = capsys.readouterr().out
    assert f":{port}/" in out and "Ctrl-C" in out


# --- startup banner -------------------------------------------------------
# These cover two bugs that shipped briefly: the banner was block-buffered and
# so never reached a redirected log (which is exactly what launchd gives it),
# and a port clash raised a raw traceback instead of a usable message.


def test_busy_port_reports_a_usable_message_not_a_traceback(tmp_path, capsys, monkeypatch):
    import errno as _errno

    from cc_insights import config as config_mod, db, serve as serve_mod

    cfg = config_mod.load(tmp_path)
    db.migrate(db.connect(cfg.db_path))

    def boom(*a, **k):
        raise OSError(_errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(serve_mod, "make_server", boom)
    assert serve_mod.run(cfg, port=9999, open_browser=False) == 1
    err = capsys.readouterr().err
    assert "already in use" in err and "--port 10000" in err


def test_banner_is_flushed_and_carries_a_well_formed_url(tmp_path, capsys, monkeypatch):
    from cc_insights import config as config_mod, db, serve as serve_mod

    cfg = config_mod.load(tmp_path)
    db.migrate(db.connect(cfg.db_path))

    server = serve_mod.make_server(cfg, port=0)
    monkeypatch.setattr(serve_mod, "make_server", lambda *a, **k: server)
    monkeypatch.setattr(server, "serve_forever", lambda *a, **k: None)

    assert serve_mod.run(cfg, open_browser=False) == 0
    out = capsys.readouterr().out
    assert "CC-Insights is running at" in out
    assert "/api/summary" in out
    assert "//api/" not in out, "double slash: server.url already ends in /"
