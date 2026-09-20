"""The local read-only HTTP server behind `cci serve`.

Nine JSON endpoints (`docs/API.md`, frozen) plus the built frontend, on
`127.0.0.1` only. Nothing here is a service: it is a local viewer for a local
database, and every design choice below follows from that.

**Zero dependencies, deliberately.** `http.server` is unglamorous and entirely
sufficient for one user on loopback. A framework would buy reloading and
routing sugar at the cost of making `pip install cc-insights` a supply-chain
decision for a tool whose entire pitch is that your agent logs never leave the
machine.

**Read-only twice over.** The database is opened with `mode=ro` and
`PRAGMA query_only`, so a bug in a query cannot write; and every verb other
than GET, HEAD and OPTIONS is refused with 405 before it reaches a handler. The
data is a derived cache of files this process must never modify.

**Errors are JSON with the right status.** 400 for a filter that cannot be
parsed, 404 for a path that does not exist, 405 for a write verb, 500 for a
query that blew up. Never a 200 carrying an error: a dashboard that renders
`{"error": ...}` as an empty chart is worse than one that shows a failure.

**CORS is exactly the Vite dev server.** `http://localhost:5173` (and its
127.0.0.1 spelling) so the frontend can be developed against a live backend.
No wildcard: a page on any other origin has no business reading this.
"""

from __future__ import annotations

import errno
import json
import mimetypes
import sqlite3
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cc_insights import metrics
from cc_insights.config import Config

__all__ = ["DEFAULT_PORT", "HOST", "InsightsServer", "make_server", "run"]

HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# The Vite dev server, so `npm run dev` can talk to a real database. Requests
# from anywhere else get the first entry back, which their browser will refuse
# -- the honest outcome for an origin this server does not serve.
ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

# Where `npm run build` puts the dashboard in a source checkout. Absent in an
# installed wheel, and absent before the frontend is built; both are fine.
DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_NO_FRONTEND_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>CC-Insights</title>
<style>
 body{{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;max-width:46rem;
      margin:12vh auto;padding:0 1.5rem;color:#1a1a1a;background:#fbfbfa}}
 code{{background:#eeedea;padding:.1em .35em;border-radius:3px}}
 a{{color:#0b6}}  ul{{padding-left:1.1rem}}
 @media (prefers-color-scheme:dark){{body{{color:#e8e6e3;background:#16161a}}
  code{{background:#2a2a30}}}}
</style>
<h1>CC-Insights API is running</h1>
<p>The dashboard has not been built yet, so there is nothing to show here.
Build it and reload:</p>
<pre><code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></pre>
<p>It will be served from <code>{dist}</code>.</p>
<p>The JSON API is live in the meantime:</p>
<ul>{links}</ul>
"""


class InsightsServer(ThreadingHTTPServer):
    """A threading HTTP server that owns one read-only DB handle per thread.

    SQLite connections are not shared across threads, and a single shared
    connection behind a lock would serialize the dashboard's nine parallel
    fetches into a queue. One connection per worker thread costs a few file
    handles and keeps the page loading in one round of requests.
    """

    daemon_threads = True          # a hung request must never block shutdown
    allow_reuse_address = True
    # A dashboard opens with nine parallel fetches plus its assets, and the
    # stdlib default backlog of 5 resets the rest before they are accepted.
    request_queue_size = 128

    def __init__(
        self,
        cfg: Config,
        *,
        host: str = HOST,
        port: int = DEFAULT_PORT,
        dist_dir: Path | None = DIST_DIR,
        quiet: bool = False,
    ) -> None:
        self.cfg = cfg
        self.dist_dir = Path(dist_dir) if dist_dir is not None else None
        self.quiet = quiet
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        super().__init__((host, port), _Handler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/"

    def conn(self) -> sqlite3.Connection:
        """This thread's read-only connection, opened on first use."""
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            return existing
        # as_uri() percent-encodes, so a database path containing a space or a
        # '?' cannot be misread as the start of the URI query.
        uri = f"{Path(self.cfg.db_path).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        self._local.conn = conn
        with self._lock:
            self._conns.append(conn)
        return conn

    def server_close(self) -> None:
        super().server_close()
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:       # a connection whose thread died mid-query
                pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "cc-insights"
    sys_version = ""
    protocol_version = "HTTP/1.1"   # keep-alive; every response carries a length

    server: InsightsServer          # narrows the base class annotation
    _responded = False              # set once a status line has gone out

    # ---------------------------------------------------------------- verbs --
    def do_GET(self) -> None:
        self._dispatch(body=True)

    def do_HEAD(self) -> None:
        self._dispatch(body=False)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.end_headers()

    def __getattr__(self, name: str):
        """Answer every verb this server does not implement with 405.

        `BaseHTTPRequestHandler` dispatches by looking up `do_<VERB>` and sends
        a 501 when it is missing. Synthesising the attribute here means POST,
        PUT, DELETE and anything exotic all land on one deliberate refusal
        rather than on the base class's HTML 501. Defined methods are found by
        normal lookup and never reach this.
        """
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def _method_not_allowed(self) -> None:
        # `Connection: close` because the refused request may carry a body this
        # handler never read; on a kept-alive socket those bytes would be
        # parsed as the next request.
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": f"{self.command} is not allowed; this server is read-only"},
            extra=(("Allow", "GET, HEAD, OPTIONS"), ("Connection", "close")),
        )

    # ------------------------------------------------------------- routing --
    def _dispatch(self, *, body: bool) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api" or path.startswith("/api/"):
                self._api(path, parsed.query, body=body)
            else:
                self._static(path, body=body)
        except ConnectionError:     # the browser navigated away mid-response
            self.close_connection = True
        except Exception as exc:    # noqa: BLE001 - a 500 must still be JSON
            self.log_error("%s while serving %s", exc.__class__.__name__, self.path)
            if self._responded:
                # A status line is already on the wire; a second one would be
                # framed as the next response. Drop the connection instead.
                self.close_connection = True
                return
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": f"{exc.__class__.__name__}: {exc}"}, body=body)

    def _api(self, path: str, query: str, *, body: bool) -> None:
        name = path[len("/api/"):] if path.startswith("/api/") else ""
        if name not in metrics.ENDPOINTS:
            known = ", ".join(f"/api/{n}" for n in metrics.ENDPOINTS)
            self._send_json(HTTPStatus.NOT_FOUND,
                            {"error": f"unknown endpoint {path!r}; try one of: {known}"},
                            body=body)
            return
        try:
            filters = metrics.Filters.from_query(parse_qs(query, keep_blank_values=True))
        except metrics.FilterError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, body=body)
            return

        data = metrics.endpoint(name, self.server.conn(), filters, self.server.cfg)
        self._send_json(HTTPStatus.OK, data, body=body)

    def _static(self, path: str, *, body: bool) -> None:
        dist = self.server.dist_dir
        target = _resolve_static(dist, path) if dist is not None else None
        if target is None:
            if path == "/":
                self._send(HTTPStatus.OK, _no_frontend_page(dist), "text/html; charset=utf-8",
                           body=body)
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": f"not found: {path}"}, body=body)
            return

        ctype = _content_type(target)
        # The database changes under a running server, so nothing it serves may
        # be cached past the request that asked for it.
        self._send(HTTPStatus.OK, target.read_bytes(), ctype, body=body,
                   extra=(("Cache-Control", "no-cache"),))

    # -------------------------------------------------------------- output --
    def _send_json(self, status: HTTPStatus, payload: Any, *, body: bool = True,
                   extra: tuple[tuple[str, str], ...] = ()) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", body=body, extra=extra)

    def _send(self, status: HTTPStatus, payload: bytes, ctype: str, *, body: bool = True,
              extra: tuple[tuple[str, str], ...] = ()) -> None:
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in extra:
            self.send_header(key, value)
        self._cors_headers()
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = origin if origin in ALLOWED_ORIGINS else ALLOWED_ORIGINS[0]
        self.send_header("Access-Control-Allow-Origin", allowed)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    # ------------------------------------------------------------- logging --
    def log_message(self, fmt: str, *args) -> None:
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def log_error(self, fmt: str, *args) -> None:
        # Errors are worth hearing about even in a quiet server.
        super().log_message(fmt, *args)


# Types a bundler emits that the stdlib map has been late to learn. Serving an
# ES module as octet-stream makes the browser refuse it, which looks like a
# broken build rather than a wrong header.
_EXTRA_TYPES = {
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json",
}


def _content_type(target: Path) -> str:
    ctype = _EXTRA_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
    ctype = ctype or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                              "image/svg+xml"):
        ctype += "; charset=utf-8"
    return ctype


def _resolve_static(dist: Path | None, path: str) -> Path | None:
    """The file under `dist` that `path` names, or None.

    Returns None for a traversal attempt, a missing file, or a missing build,
    so the caller renders one honest 404 for every "there is nothing here".
    There is no single-page fallback: an unknown path is a 404, which is what
    the contract promises and what makes a typo in a fetch URL visible.
    """
    if dist is None or not dist.is_dir():
        return None
    rel = path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    root = dist.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


def _no_frontend_page(dist: Path | None) -> bytes:
    links = "".join(f'<li><a href="/api/{n}">/api/{n}</a></li>' for n in metrics.ENDPOINTS)
    return _NO_FRONTEND_HTML.format(
        dist=dist if dist is not None else "frontend/dist", links=links
    ).encode("utf-8")


def make_server(
    cfg: Config,
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    dist_dir: Path | None = DIST_DIR,
    quiet: bool = False,
) -> InsightsServer:
    """Bind the server without serving. `port=0` picks a free port."""
    return InsightsServer(cfg, host=host, port=port, dist_dir=dist_dir, quiet=quiet)


def run(
    cfg: Config,
    *,
    host: str = HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    dist_dir: Path | None = DIST_DIR,
    quiet: bool = False,
) -> int:
    """Serve until interrupted. Returns a process exit code."""
    try:
        server = make_server(cfg, host=host, port=port, dist_dir=dist_dir, quiet=quiet)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            print(f"port {port} is already in use — try `cci serve --port {port + 1}`",
                  file=sys.stderr, flush=True)
            return 1
        raise

    built = server.dist_dir is not None and server.dist_dir.is_dir()

    # flush=True on every line: launchd and `cci serve > log` redirect stdout to
    # a file, where print is block-buffered and the banner would otherwise never
    # reach the log.
    say = lambda line="": print(line, flush=True)
    say()
    say(f"  CC-Insights is running at  {server.url}")
    say()
    say(f"  database   {cfg.db_path}")
    if built:
        say(f"  frontend   {server.dist_dir}")
    else:
        say("  frontend   not built — serving the JSON API only")
        say("             build it with:  cd frontend && pnpm install && pnpm build")
    base = server.url.rstrip("/")
    say(f"  endpoints  {base}/api/summary  (see docs/API.md for all {len(metrics.ENDPOINTS)})")
    say()
    say("  Ctrl-C to stop")
    say()
    if open_browser:
        # After the loop is accepting, so the first request is not refused.
        threading.Timer(0.3, webbrowser.open, args=(server.url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        # serve_forever() has already left its loop; only the sockets and the
        # per-thread database handles are still open.
        server.server_close()
    return 0
