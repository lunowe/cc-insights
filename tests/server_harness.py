"""Stand up the real account server for the client tests. Not a test module.

A mocked HTTP layer proves the client talks to itself. The interesting
failures in `remote.py` are the ones only a real server produces: a 400 whose
body names an unknown column, a keyset cursor that has to survive being
handed back verbatim, an upsert that collides on a composite primary key, a
`slow_down` the server decides on its own clock.

So this spawns `tests/_e2e_server.py` under `server/.venv`, against a
throwaway PostgreSQL database, and tears both down afterwards.

It **skips** when the server venv or PostgreSQL is missing, and never falls
back to a substitute -- `server/tests/conftest.py` takes the same line and
gives the reason: a green run against a stand-in is a worse outcome than a
skipped one, because only one of the two is honest about what was checked.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
SERVER_PYTHON = SERVER_DIR / ".venv" / "bin" / "python"
RUNNER = Path(__file__).resolve().parent / "_e2e_server.py"

ADMIN_URL = os.environ.get("CCI_SERVER_TEST_ADMIN_URL", "postgresql:///postgres")

#: How long to wait for uvicorn to answer /healthz. Generous because the
#: first start also applies every migration against a fresh database.
BOOT_TIMEOUT_S = 40.0

#: The device flow's lifetime here. Short on purpose: it is the backstop that
#: turns a client that never stops polling into a failing test in a minute
#: rather than a suite that hangs until somebody notices.
DEVICE_TTL_S = 60


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _psql(sql: str, url: str = ADMIN_URL) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["psql", url, "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True,
    )


def requirements() -> str | None:
    """The reason this cannot run, or None. Checked once, reported precisely."""
    if not SERVER_PYTHON.exists():
        return (f"no server virtualenv at {SERVER_PYTHON}. "
                "See server/README.md: uv venv .venv && uv pip install -e '.[dev]'")
    probe = subprocess.run(["pg_isready"], capture_output=True, text=True)
    if probe.returncode != 0:
        return f"no PostgreSQL reachable ({probe.stdout.strip() or 'pg_isready failed'})"
    if _psql("SELECT 1").returncode != 0:
        return f"cannot connect to {ADMIN_URL!r}; set CCI_SERVER_TEST_ADMIN_URL"
    return None


@dataclass
class LiveServer:
    """A running account server, plus the handful of controls a test needs."""

    base_url: str
    database_url: str
    _process: subprocess.Popen
    _dbname: str

    # -- the harness's own control surface --------------------------------

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"{}")

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as resp:
            return json.loads(resp.read() or b"{}")

    def latest_device(self) -> str:
        """The upstream device code of the most recently started flow.

        `FakeProvider` names them `gh-device-1`, `gh-device-2`, ... and the
        counter lives in the server process, so a test cannot predict it
        across a shared server. Asking is more robust than counting, and the
        client has already had to start the flow before there is anything to
        script anyway.
        """
        devices = self._get("/test/provider/devices")["devices"]
        assert devices, "no device flow has been started yet"
        return max(devices, key=lambda d: int(d.rsplit("-", 1)[1]))

    def register_identity(self, token: str, subject: str, login: str,
                          repos: list[str] | None = None) -> None:
        """Teach the scripted GitHub who a granted token belongs to."""
        self._post("/test/provider/identity", {
            "token": token, "subject": subject, "login": login,
            "repos": list(repos or []),
        })

    def script_device(self, device: str, results: list[dict]) -> None:
        """Queue what GitHub will answer for one upstream device code.

        `FakeProvider` names them `gh-device-1`, `gh-device-2`, ... in the
        order flows are started, which is deterministic enough to script
        before the client has even asked for one.
        """
        self._post("/test/provider/script", {"device": device, "results": results})

    def truncate(self) -> None:
        """Empty every table, so one test cannot see another's rows."""
        tables = [
            "published_span", "published_session_branch", "published_session",
            "repo_publisher", "team_repo", "published_repo", "published_withheld",
            "span", "event", "thread", "session", "project_probe", "project",
            "project_group", "host", "account_repo_access", "device_authorization",
            "api_token", "team_member", "team", "identity", "account",
        ]
        done = _psql("TRUNCATE " + ", ".join(tables) + " CASCADE", self.database_url)
        assert done.returncode == 0, done.stderr

    def stop(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:      # pragma: no cover - slow shutdown
            self._process.kill()
            self._process.wait(timeout=10)
        _psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{self._dbname}' AND pid <> pg_backend_pid()"
        )
        _psql(f'DROP DATABASE IF EXISTS "{self._dbname}"')


def start(device_ttl_s: int = DEVICE_TTL_S) -> LiveServer:
    """Create a database, boot the server on a free port, wait for /healthz."""
    reason = requirements()
    if reason:
        pytest.skip(reason)

    dbname = f"cci_client_e2e_{os.getpid()}_{int(time.time() * 1000) % 1_000_000}"
    created = _psql(f'CREATE DATABASE "{dbname}"')
    assert created.returncode == 0, created.stderr
    database_url = ADMIN_URL.rsplit("/", 1)[0] + "/" + dbname

    port = _free_port()
    process = subprocess.Popen(
        [str(SERVER_PYTHON), str(RUNNER),
         "--database-url", database_url, "--port", str(port),
         "--device-ttl", str(device_ttl_s)],
        cwd=str(SERVER_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    server = LiveServer(base_url, database_url, process, dbname)

    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            _psql(f'DROP DATABASE IF EXISTS "{dbname}"')
            pytest.fail(f"the account server exited before it was ready:\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/healthz", timeout=2) as resp:
                if resp.status == 200:
                    return server
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.2)

    server.stop()                                  # pragma: no cover - slow boot
    pytest.fail(f"the account server did not answer /healthz within {BOOT_TIMEOUT_S}s")
