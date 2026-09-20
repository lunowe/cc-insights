"""The account server, over HTTP. Stdlib only.

`docs/SERVER_API.md` is the contract and it is frozen; this module is the
client half of it. `sync.py` is the other transport and still the right one
for anybody running their own PostgreSQL -- `docs/ACCOUNTS.md` §3 keeps that
a supported mode rather than dead code. What changes here is the pipe, not
the semantics: the ownership rules, the conflict clauses and the excluded
tables are `sync.TABLES` and `sync.EXCLUDED`, unchanged.

**urllib, not requests.** The base install has no dependencies and
`test_packaging.py::test_the_base_install_has_no_required_dependencies`
enforces it. A sign-in that costs somebody a wheel build is a sign-in that
does not happen on the machine that needed it.

Three properties are load-bearing and each one cost a design decision.

**Nothing is held in memory.** The measured corpus is 191,475 rows. Push
streams off a SQLite cursor a batch at a time and pull writes each page
before asking for the next, so peak memory is one batch either way.

**A network blip does not restart from zero.** Progress is written to
`remote-state.json` after every acknowledged batch, guarded by a digest of
the table's contents: a resumed push continues where it stopped, and a table
that changed underneath an abandoned run starts over rather than skipping the
rows that moved. The same digest makes an unchanged table cost zero requests,
which is what keeps a 15-minute background job from re-uploading 187k events
forever.

**The projection is computed here.** `publish` sends `redact.publication()`
and nothing else. There is no endpoint that takes a raw row and filters it
server-side, because `docs/ACCOUNTS.md` §2 rejects exactly that: the shared
store cannot leak a path it never received.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from cc_insights import __version__, redact, sync

#: Server-side cap from docs/SERVER_API.md §0. Sending more is 413, not a
#: truncation, so the client has to know the number rather than discover it.
MAX_BATCH_ROWS = 5000

#: What we actually send. Below the cap on purpose: a batch is one
#: transaction on the server and one retry unit here, and a failed 5000-row
#: `event` request costs more to redo than two 2000-row ones.
PUSH_BATCH = 2000
PULL_PAGE = 1000

DEFAULT_TIMEOUT_S = 30.0

#: Device-flow error codes, RFC 8628 §3.5 as the contract restates them.
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
EXPIRED = "expired_token"
DENIED = "access_denied"
INVALID_DEVICE_CODE = "invalid_device_code"
UPSTREAM_UNAVAILABLE = "upstream_unavailable"

#: Stop. The other codes mean wait and poll again.
TERMINAL_CODES = frozenset({EXPIRED, DENIED, INVALID_DEVICE_CODE})

#: Added to every wait between polls. The server throttles on its own clock
#: (`device.poll` refuses a poll arriving sooner than `interval` ms after the
#: last one) and sleeping *exactly* the interval races that comparison across
#: two machines' clocks. Losing the race costs a `slow_down`, and the penalty
#: is sticky -- the interval rises by 5 seconds for the rest of the flow.
#: Half a second to avoid a five-second regression is not a close call.
POLL_MARGIN_S = 0.5


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class RemoteError(RuntimeError):
    """Anything the account server refused, in a form a CLI can print.

    `str()` is the whole message a user should see. The rule the rest of this
    project follows -- every failure names the command that fixes it -- is why
    `AuthRequired` is a separate class rather than a status code somebody has
    to remember to branch on.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, retry_after: int | None = None,
                 payload: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.payload = payload or {}


class AuthRequired(RemoteError):
    """401. The credential is missing, unknown, expired or revoked.

    Distinct from `Forbidden` because the correct response differs and a user
    cannot be expected to know that: this one is fixed by signing in again,
    and the other never is.
    """


class Forbidden(RemoteError):
    """403. Authenticated, allowed to know the thing exists, not allowed to do it."""


class NotFound(RemoteError):
    """404 -- which the contract also uses for "exists, but not yours".

    §0: a distinguishable 403 would leak the existence of the row it refused.
    So this must never be reported as "you lack permission for X"; it is
    "there is no X".
    """


class Conflict(RemoteError):
    """409. Foreign-key order, last admin, or an id owned by somebody else."""


class BatchTooLarge(RemoteError):
    """413. The cap is in `message`, and this client should never provoke it."""


class Unreachable(RemoteError):
    """The request never got a usable HTTP answer. Usually transient, never fatal.

    Deliberately also covers 502 and 503: from a CLI's point of view "GitHub
    is down" and "the server's database is down" are the same instruction --
    wait and retry -- and neither is something the user can fix by typing a
    different command.
    """


class DeviceFlowError(RemoteError):
    """A terminal device-flow outcome: expired, denied, or an unusable code."""


class SchemaDrift(RemoteError):
    """The server wants a column this client cannot produce.

    A hardcoded copy of the transfer rules is how `sync`'s own first test
    passed through a merge that added three tables. `GET /v1/personal/tables`
    exists so this is an error at the start of a push rather than a silently
    wrong row at the end of one.
    """


_STATUS_ERRORS: dict[int, type[RemoteError]] = {
    401: AuthRequired,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    413: BatchTooLarge,
}


# --------------------------------------------------------------------------
# wire shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceFlow:
    """What `POST /v1/auth/device/start` handed back. Print both codes."""

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: int
    interval: int


@dataclass(frozen=True)
class Identity:
    """A completed sign-in. `token` is shown once by the server and never again."""

    token: str = field(repr=False)
    account_id: str
    actor: str
    expires_at: int | None = None


@dataclass(frozen=True)
class Poll:
    """One device-flow poll. Exactly one of `identity` and `error` is set."""

    identity: Identity | None = None
    error: str | None = None
    message: str = ""
    #: The server's current minimum seconds between polls, when it said. Present
    #: on both retryable codes, so a client reading only this still converges.
    interval: int | None = None


@dataclass(frozen=True)
class BatchResult:
    """What one push or publish request reported.

    `applied` counts rows sent and accepted, not rows that changed. Knowing
    what actually changed would mean reading every row back, which costs more
    than the push -- the same reasoning as `sync.SyncStats`.
    """

    received: int = 0
    applied: int = 0
    rejected: int = 0


# --------------------------------------------------------------------------
# the HTTP client
# --------------------------------------------------------------------------


class Client:
    """One account server, one credential.

    Every method raises a `RemoteError` subclass and never a traceback out of
    `urllib`; a stack trace is not an answer to "the server said no".
    """

    def __init__(self, base_url: str, token: str | None = None, *,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 opener: Callable[..., Any] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        # Injectable so a unit test can drive the transport without a socket.
        # The end-to-end suite deliberately does NOT use it: it runs a real
        # server, because a mocked HTTP layer proves the client talks to itself.
        self._open = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))

    # -- transport ---------------------------------------------------------

    def _url(self, path: str, params: Sequence[tuple[str, Any]] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            pairs = [(k, v) for k, v in params if v is not None]
            if pairs:
                url += "?" + urllib.parse.urlencode(pairs, doseq=True)
        return url

    def request(self, method: str, path: str, *,
                params: Sequence[tuple[str, Any]] | None = None,
                body: Any = None, authenticated: bool = True) -> Any:
        """One round trip. Returns the decoded body, or None for a 204."""
        if authenticated and not self.token:
            # Caught before the socket: "not signed in" is not a server
            # opinion, and a 401 round trip to learn it is a slower way to
            # print the same sentence.
            raise AuthRequired(
                "not signed in, and this needs an account.\n"
                "    run `cci login` to sign in."
            )

        data = None
        headers = {"Accept": "application/json", "User-Agent": f"cc-insights/{__version__}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(self._url(path, params), data=data,
                                     headers=headers, method=method)
        try:
            with self._open(req, self.timeout_s) as resp:
                return _decode(resp)
        except urllib.error.HTTPError as exc:
            # An HTTPError IS the response, so the error envelope the contract
            # guarantees is readable here. Reading it is the difference between
            # "HTTP Error 409" and a sentence naming the table.
            payload = _decode(exc) or {}
            raise _error_for(exc.code, payload, exc.headers, self.base_url, path) from None
        except urllib.error.URLError as exc:
            raise Unreachable(_unreachable(self.base_url, exc.reason)) from None
        except (TimeoutError, OSError) as exc:
            raise Unreachable(_unreachable(self.base_url, exc)) from None

    # -- health and identity ----------------------------------------------

    def healthz(self) -> dict:
        return self.request("GET", "/healthz", authenticated=False)

    def whoami(self) -> dict:
        return self.request("GET", "/v1/auth/whoami")

    def logout(self) -> None:
        self.request("POST", "/v1/auth/logout")

    def tokens(self) -> list[dict]:
        return self.request("GET", "/v1/auth/tokens")["tokens"]

    # -- sign-in -----------------------------------------------------------

    def device_start(self, client_name: str | None = None) -> DeviceFlow:
        body = self.request("POST", "/v1/auth/device/start",
                            body={"clientName": client_name}, authenticated=False)
        return DeviceFlow(
            device_code=body["deviceCode"],
            user_code=body["userCode"],
            verification_uri=body["verificationUri"],
            verification_uri_complete=(body.get("verificationUriComplete")
                                       or body["verificationUri"]),
            expires_at=int(body["expiresAt"]),
            interval=int(body.get("interval") or 5),
        )

    def device_poll(self, device_code: str) -> Poll:
        """One poll, with every documented outcome turned into a `Poll`.

        The four RFC 8628 codes arrive as 400s, so they are *not* exceptions
        here -- three of them are the normal path of a flow nobody has
        approved yet, and raising on the normal path would make the caller
        catch its way through a state machine.
        """
        try:
            body = self.request("POST", "/v1/auth/device/token",
                                body={"deviceCode": device_code}, authenticated=False)
        except RemoteError as exc:
            interval = exc.payload.get("interval") or exc.retry_after
            return Poll(error=exc.code or "error", message=str(exc),
                        interval=int(interval) if interval else None)
        return Poll(identity=Identity(
            token=body["accessToken"],
            account_id=body["accountId"],
            actor=body["actor"],
            expires_at=body.get("expiresAt"),
        ))

    def device_await(self, flow: DeviceFlow, *,
                     sleep: Callable[[float], None] = time.sleep,
                     now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                     on_wait: Callable[[int, str], None] | None = None) -> Identity:
        """Poll until the person approves, declines, or the flow dies.

        Both sources of `slow_down` are honoured, and the contract (§2) is
        explicit that there are two: GitHub throttling this server, and this
        server throttling an impatient CLI before it forwards anything to
        GitHub at all. The second exists because one CLI polling too fast
        exhausts the instance's GitHub rate limit and then *nobody* can sign
        in. So the interval only ever rises, and it rises to whatever the
        server last said rather than to whatever this client guessed.

        A `502 upstream_unavailable` is waited out rather than raised. It is
        explicitly retryable in the contract, and a login that aborts because
        GitHub had a bad second is a login the user has to notice and redo.
        """
        interval = max(1, flow.interval)
        while True:
            poll = self.device_poll(flow.device_code)
            if poll.identity is not None:
                return poll.identity

            if poll.error in TERMINAL_CODES:
                raise DeviceFlowError(_terminal_message(poll), code=poll.error)

            if poll.interval:
                # Never down. A server that has raised the interval has a
                # reason, and a client that lowers it again re-earns the raise.
                interval = max(interval, poll.interval)

            if now_ms() >= flow.expires_at:
                raise DeviceFlowError(
                    "the sign-in request expired before it was approved.\n"
                    "    run `cci login` again.",
                    code=EXPIRED,
                )

            if on_wait is not None:
                on_wait(interval, poll.error or "")
            sleep(interval + POLL_MARGIN_S)

    # -- the personal store ------------------------------------------------

    def personal_tables(self) -> dict:
        """The server's own copy of the transfer rules, so drift is detectable."""
        return self.request("GET", "/v1/personal/tables")

    def personal_status(self) -> dict:
        return self.request("GET", "/v1/personal/status")

    def personal_push(self, host_id: str, table: str, columns: Sequence[str],
                      rows: Sequence[Sequence[Any]]) -> BatchResult:
        body = self.request("POST", "/v1/personal/push", body={
            "hostId": host_id,
            "table": table,
            "columns": list(columns),
            "rows": [list(r) for r in rows],
        })
        return BatchResult(received=body.get("received", 0),
                           applied=body.get("applied", 0),
                           rejected=body.get("rejected", 0))

    def personal_pull(self, table: str, *, limit: int = PULL_PAGE,
                      cursor: str | None = None) -> dict:
        return self.request("GET", "/v1/personal/pull", params=[
            ("table", table), ("limit", limit), ("cursor", cursor),
        ])

    # -- the team store ----------------------------------------------------

    def team_publish(self, kind: str, actor: str, rows: Sequence[dict]) -> BatchResult:
        body = self.request("POST", "/v1/team/publish", body={
            "kind": kind, "actor": actor, "rows": list(rows),
        })
        return BatchResult(received=body.get("received", 0),
                           applied=body.get("applied", 0),
                           rejected=body.get("rejected", 0))

    def team_repos(self) -> list[dict]:
        return self.request("GET", "/v1/team/repos")["repos"]

    def team_summary(self, **filters: Any) -> dict:
        return self.request("GET", "/v1/team/summary", params=_filter_params(filters))

    def team_sessions(self, *, limit: int = PULL_PAGE, cursor: str | None = None,
                      **filters: Any) -> dict:
        params = _filter_params(filters) + [("limit", limit), ("cursor", cursor)]
        return self.request("GET", "/v1/team/sessions", params=params)

    def team_actors(self, **filters: Any) -> dict:
        return self.request("GET", "/v1/team/actors", params=_filter_params(filters))

    def team_daily(self, **filters: Any) -> list[dict]:
        return self.request("GET", "/v1/team/daily", params=_filter_params(filters))["days"]

    def teams(self) -> list[dict]:
        return self.request("GET", "/v1/teams")["teams"]


def _unreachable(base_url: str, reason: Any) -> str:
    return (f"cannot reach the account server at {base_url}: {reason}\n"
            "    local capture is unaffected; this only delays sharing.")


def _decode(resp) -> Any:
    raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _error_for(status: int, payload: dict, headers, base_url: str, path: str) -> RemoteError:
    """The contract's error envelope, as the exception a CLI should print.

    Every non-2xx body is `{"error", "message"}` and nothing else (§0), so
    there is exactly one shape to read. What the messages below add is the
    *command*, which the server cannot know.
    """
    code = payload.get("error") if isinstance(payload, dict) else None
    message = payload.get("message") if isinstance(payload, dict) else None
    retry_after = _int_or_none(headers.get("Retry-After") if headers else None)

    if status == 401:
        return AuthRequired(
            "the account server did not accept this credential.\n"
            "    run `cci login` to sign in again.",
            code=code or "unauthenticated", status=status, payload=payload)
    if status == 503:
        return Unreachable(
            f"the account server at {base_url} cannot reach its database.\n"
            "    nothing was sent; local capture is unaffected. Try again shortly.",
            code=code, status=status, retry_after=retry_after, payload=payload)
    if status == 502:
        return Unreachable(
            f"{message or 'the account server could not reach GitHub.'}\n"
            "    this is usually over in a minute. Try again.",
            code=code or UPSTREAM_UNAVAILABLE, status=status,
            retry_after=retry_after, payload=payload)
    if status == 429:
        wait = f" Wait {retry_after}s." if retry_after else ""
        return RemoteError(f"the account server is rate limiting this client.{wait}",
                           code=code or "rate_limited", status=status,
                           retry_after=retry_after, payload=payload)

    cls = _STATUS_ERRORS.get(status, RemoteError)
    text = message or f"the account server refused {path} with HTTP {status}."
    return cls(text, code=code, status=status, retry_after=retry_after, payload=payload)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _terminal_message(poll: Poll) -> str:
    if poll.error == DENIED:
        return "the sign-in was declined in the browser. Nothing was stored."
    if poll.error == EXPIRED:
        return ("the sign-in request expired before it was approved.\n"
                "    run `cci login` again.")
    return ("that sign-in request is no longer usable.\n"
            "    run `cci login` to start a new one.")


def _filter_params(filters: dict[str, Any]) -> list[tuple[str, Any]]:
    """The §4.3 filter set, repeated params and all.

    `from` is a Python keyword, so callers pass `frm`; everything else keeps
    the wire name. An unsupported filter raises rather than being dropped,
    because a filter that silently does nothing returns somebody else's
    numbers under the caller's question.
    """
    wire = {"repo": "repo", "actor": "actor", "source": "source", "role": "role",
            "frm": "from", "to": "to"}
    out: list[tuple[str, Any]] = []
    for name, value in filters.items():
        if value is None:
            continue
        if name not in wire:
            raise ValueError(f"unknown team filter {name!r}; expected {sorted(wire)}")
        if isinstance(value, (list, tuple, set)):
            out.extend((wire[name], v) for v in value)
        else:
            out.append((wire[name], value))
    return out


# --------------------------------------------------------------------------
# what travels: `sync.TABLES` reconciled with what the server says it takes
# --------------------------------------------------------------------------

#: Columns `sync.TABLES` carries that the personal store deliberately does not
#: model, and why dropping each one is correct rather than lossy. Anything
#: dropped that is NOT listed here is a `SchemaDrift`, because "silently
#: dropped value" is the failure the contract refuses on its own side (§3) and
#: a client that does it quietly is no better.
WIRE_OMITTED: dict[tuple[str, str], str] = {
    ("host", "account_id"): (
        "the server reads the account from the bearer token, and every key in "
        "the personal store is already composite on it. A client sending its "
        "own account id would be a tenant asserting its own tenancy, which is "
        "the one thing a tenant must not get to say."
    ),
}


@dataclass(frozen=True)
class TablePlan:
    """One table's transfer, after the client and the server have agreed.

    Built from `GET /v1/personal/tables` rather than from a hardcoded copy.
    The contract offers that endpoint precisely so drift is an error at the
    start of a push instead of a wrong row at the end of one -- and `sync`'s
    own first test proved the alternative, sailing through a merge that added
    three tables because it compared the module to a copy of itself.
    """

    table: sync.Table
    columns: tuple[str, ...]
    omitted: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.table.name


def plan_tables(server_tables: dict) -> list[TablePlan]:
    """Reconcile `sync.TABLES` with the server's declared rules, in push order."""
    declared = {t["table"]: t for t in server_tables.get("tables", [])}
    plans: list[TablePlan] = []
    for table in sync.TABLES:
        spec = declared.get(table.name)
        if spec is None:
            raise SchemaDrift(
                f"this client pushes {table.name!r} and the account server does not "
                "transfer it. Upgrade whichever of the two is older."
            )
        required = list(spec.get("columns") or ())
        optional = set(spec.get("optionalColumns") or ())
        known = set(required) | optional

        missing = [c for c in required if c not in table.columns]
        if missing:
            raise SchemaDrift(
                f"the account server requires {table.name} columns {missing}, which "
                "this client does not have. Upgrade cc-insights."
            )

        send = tuple(c for c in table.columns if c in known)
        omitted = tuple(c for c in table.columns if c not in known)
        for column in omitted:
            if (table.name, column) not in WIRE_OMITTED:
                raise SchemaDrift(
                    f"the account server has no {table.name}.{column}, and this client "
                    "will not drop a value quietly. Upgrade the server, or add the "
                    "column to remote.WIRE_OMITTED with the reason it is safe."
                )
        plans.append(TablePlan(table=table, columns=send, omitted=omitted))
    return plans


def wire_upsert_sql(table: sync.Table, columns: Sequence[str]) -> str:
    """An upsert over exactly the columns received, for the local SQLite side.

    A column that did not arrive is left out of the SET list entirely, so the
    stored value survives. That is the contract's "an absent optional column
    is left untouched rather than nulled" (§3), implemented as the only thing
    it can honestly be: not mentioning the column.
    """
    columns = list(columns)
    fragments = sync.set_fragments(table)
    sets = [fragments.get(c, f"{c} = excluded.{c}")
            for c in columns if c not in table.key]
    action = f"DO UPDATE SET {', '.join(sets)}" if sets else "DO NOTHING"
    return (
        f"INSERT INTO {table.name} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT ({', '.join(table.key)}) {action}"
    )


# --------------------------------------------------------------------------
# resume state
# --------------------------------------------------------------------------

#: Bookkeeping about a transfer, beside the config rather than inside the
#: database. `ingest_file` is the precedent and `sync.EXCLUDED` states the
#: rule it follows: how far this machine got is a fact about this machine, so
#: it must not become a row other machines pull down. Keeping it out of the
#: schema also keeps it out of `sync.TABLES`, `redact.FIELDS` and every
#: coverage test that would otherwise have to rule on a column that describes
#: nobody's work.
STATE_FILE = "remote-state.json"
STATE_VERSION = 1


def state_path(config_dir: Path) -> Path:
    return Path(config_dir).expanduser() / STATE_FILE


def load_state(config_dir: Path) -> dict:
    """The stored transfer state, or an empty one. Never raises.

    A corrupt state file must cost a re-push, not a broken command: every id
    is a content hash, so the worst case of forgetting where we were is
    sending rows that land on themselves.
    """
    try:
        with state_path(config_dir).open("rb") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "targets": {}}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {"version": STATE_VERSION, "targets": {}}
    data.setdefault("targets", {})
    return data


def save_state(config_dir: Path, state: dict) -> None:
    """Write atomically, so an interrupted save cannot lose the whole record."""
    path = state_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".remote-state-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, indent=1, sort_keys=True)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def target_key(base_url: str, account_id: str) -> str:
    """One record per (server, account).

    Both halves matter. Signing in as somebody else against the same server
    must not inherit the previous account's "already pushed" marks, and the
    same account on two servers has genuinely pushed different amounts.
    """
    return f"{base_url.rstrip('/')}|{account_id}"


def _target(state: dict, key: str) -> dict:
    return state.setdefault("targets", {}).setdefault(key, {})


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------


@dataclass
class TransferStats:
    """What moved, what was resumed, and what did not have to move at all."""

    direction: str = "push"
    rows: dict[str, int] = field(default_factory=dict)
    #: Tables whose contents are byte-identical to the last completed transfer.
    skipped: list[str] = field(default_factory=list)
    #: table -> rows already done when this run picked up an abandoned one.
    resumed: dict[str, int] = field(default_factory=dict)
    requests: int = 0

    @property
    def total(self) -> int:
        return sum(self.rows.values())


def _row_bytes(row: Sequence[Any]) -> bytes:
    """One row, canonically, for the digest.

    `\\x00` for NULL and `\\x1f` between cells, the same separators `ids.make_id`
    uses and for the same reason: a NULL and the empty string must not hash
    alike, or a column being cleared would look like no change and the table
    would be skipped.
    """
    return ("\x1f".join("\x00" if v is None else str(v) for v in row) + "\x1e").encode(
        "utf-8", "surrogatepass"
    )


def table_digest(conn: sqlite3.Connection, plan: TablePlan, host_id: str) -> tuple[str, int]:
    """A hash of everything this host owns in one table, and the row count.

    This is what makes a re-push free. The alternative -- a row count, or a max
    timestamp -- is cheaper and wrong here: `cci backfill` fills
    `event.cache_write_1h_tokens` in place without changing an id, a count or a
    timestamp, and on the author's corpus 41% of cache-write tokens bought a
    one-hour TTL. A table skipped on those grounds would keep the five-minute
    price on the server forever.

    It costs one local scan. Reading 187k rows out of SQLite is tens of
    milliseconds; the thing being avoided is ~95 HTTP requests.
    """
    digest = hashlib.sha256()
    digest.update(("\x1e".join(plan.columns) + "\x1d").encode("utf-8"))
    count = 0
    for chunk, _cursor in _local_batches(conn, plan, host_id, None, 4096):
        for row in chunk:
            digest.update(_row_bytes(row))
            count += 1
    return digest.hexdigest(), count


def _keyset_where(keys: Sequence[str]) -> str:
    """`(k1, k2) > (?, ?)`, spelled out.

    SQLite has supported row values since 3.15, but writing the expansion
    keeps the statement readable in a log and costs nothing.
    """
    parts = []
    for i, key in enumerate(keys):
        equals = " AND ".join(f"{k} = ?" for k in keys[:i])
        greater = f"{key} > ?"
        parts.append(f"({equals} AND {greater})" if equals else f"({greater})")
    return "(" + " OR ".join(parts) + ")"


def _keyset_params(after: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for i in range(len(after)):
        out.extend(after[:i])
        out.append(after[i])
    return out


def _local_batches(conn: sqlite3.Connection, plan: TablePlan, host_id: str,
                   cursor: dict | None, batch: int) -> Iterator[tuple[list[tuple], dict]]:
    """This host's rows for one table, in insertable order, `batch` at a time.

    Two strategies, because one table cannot use the other's.

    Everything except `thread` is streamed by keyset over the table's own key,
    so peak memory is one batch no matter how large `event` grows, and the
    cursor is a real resume point.

    `thread` references itself, and a child in batch 1 whose parent lands in
    batch 2 is a `409 foreign_key_violation` -- the contract defers the
    self-reference *within* one request, not across two. So it is materialized
    and topologically sorted first, exactly as `sync._chunks` already does, and
    its cursor is a position in that ordering. Affordable precisely because
    threads are three orders of magnitude fewer than events.
    """
    columns = list(plan.columns)
    table = plan.table

    if table.parent is not None:
        sql = f"SELECT {', '.join(columns)} FROM {table.name}"
        params: list[Any] = []
        if table.owner_filter:
            sql += f" WHERE {table.owner_filter}"
            params.append(host_id)
        rows = sync.order_by_parent(
            table, [tuple(r) for r in conn.execute(sql, params).fetchall()], columns
        )
        start = int(cursor.get("index", 0)) if cursor else 0
        for i in range(start, len(rows), batch):
            chunk = rows[i:i + batch]
            yield chunk, {"index": i + len(chunk)}
        return

    keys = list(table.key)
    after = list(cursor["key"]) if cursor and cursor.get("key") is not None else None
    key_at = [columns.index(k) for k in keys]
    while True:
        where = []
        params = []
        if table.owner_filter:
            where.append(f"({table.owner_filter})")
            params.append(host_id)
        if after is not None:
            where.append(_keyset_where(keys))
            params.extend(_keyset_params(after))
        sql = f"SELECT {', '.join(columns)} FROM {table.name}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {', '.join(keys)} LIMIT ?"
        params.append(batch)

        chunk = [tuple(r) for r in conn.execute(sql, params).fetchall()]
        if not chunk:
            return
        after = [chunk[-1][i] for i in key_at]
        yield chunk, {"key": after}
        if len(chunk) < batch:
            return


def push(conn: sqlite3.Connection, client: Client, host_id: str, *, config_dir: Path,
         batch: int = PUSH_BATCH, force: bool = False,
         on_table: Callable[[str, str], None] | None = None) -> TransferStats:
    """Send this host's rows to the account server, resumably.

    State is written after every acknowledged batch, so a connection that
    drops at row 150,000 costs the current batch and not the 149,000 before
    it. The state is keyed by a digest of the table, which is what makes the
    two cases distinguishable that a bare cursor confuses: a run that was
    interrupted (resume) and a run that finished over data which has since
    changed (start again).
    """
    if batch > MAX_BATCH_ROWS:
        raise ValueError(f"batch {batch} is above the server's cap of {MAX_BATCH_ROWS}")

    plans = plan_tables(client.personal_tables())
    state = load_state(config_dir)
    account_id = _account_id_for(client)
    record = _target(state, target_key(client.base_url, account_id))
    tables: dict = record.setdefault("push", {}).setdefault("tables", {})

    stats = TransferStats(direction="push")
    for plan in plans:
        digest, count = table_digest(conn, plan, host_id)
        entry = tables.get(plan.name) or {}
        same = entry.get("digest") == digest and not force

        if same and entry.get("done"):
            stats.skipped.append(plan.name)
            if on_table is not None:
                on_table(plan.name, "unchanged")
            continue

        cursor = entry.get("cursor") if same else None
        # Two counters, and conflating them was a real bug: `total` is how far
        # through the table we are and belongs in the state file, `this_run`
        # is what actually crossed the wire and belongs in the report. With
        # one counter a resumed push claims to have sent the rows it
        # deliberately skipped, which is the opposite of what it did.
        total = int(entry.get("sent", 0)) if cursor else 0
        this_run = 0
        if cursor:
            stats.resumed[plan.name] = total
        if on_table is not None:
            on_table(plan.name, "resuming" if cursor else "sending")

        for chunk, at in _local_batches(conn, plan, host_id, cursor, batch):
            client.personal_push(host_id, plan.name, plan.columns, chunk)
            stats.requests += 1
            total += len(chunk)
            this_run += len(chunk)
            tables[plan.name] = {"digest": digest, "cursor": at, "sent": total,
                                 "done": False, "at": _now_ms()}
            save_state(config_dir, state)

        tables[plan.name] = {"digest": digest, "cursor": None, "sent": total,
                             "rows": count, "done": True, "at": _now_ms()}
        save_state(config_dir, state)
        if this_run:
            stats.rows[plan.name] = this_run

    record["push"]["lastSuccessAt"] = _now_ms()
    save_state(config_dir, state)
    return stats


def _account_id_for(client: Client) -> str:
    """The account this token belongs to, as the server understands it.

    Asked rather than taken from the stored credential: the state file records
    what the *server* thinks was pushed, and a stale local account id would
    file this run's progress under the wrong heading.
    """
    return client.whoami()["accountId"]


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------


def pull(conn: sqlite3.Connection, client: Client, *, config_dir: Path,
         page: int = PULL_PAGE, force: bool = False,
         on_table: Callable[[str, str], None] | None = None) -> TransferStats:
    """Bring every machine's rows on this account down into the local database.

    Not filtered by host, exactly as `sync.pull` is not: the point of a pull is
    to see the other machines. This host's own rows come back too and land on
    themselves, because the ids are hashes of the same content.

    Each page is committed before the next is requested. That is what makes an
    interrupted pull resumable -- an all-or-nothing transaction over 191k rows
    would have nothing to resume from -- and it is safe because every write is
    an upsert keyed on a content hash.
    """
    state = load_state(config_dir)
    account_id = _account_id_for(client)
    record = _target(state, target_key(client.base_url, account_id))
    tables: dict = record.setdefault("pull", {}).setdefault("tables", {})

    stats = TransferStats(direction="pull")
    for table in sync.TABLES:
        entry = tables.get(table.name) or {}
        cursor = None if (force or entry.get("done")) else entry.get("cursor")
        if on_table is not None:
            on_table(table.name, "resuming" if cursor else "receiving")

        received = 0
        # `thread` references itself, and the server pages in key order, so a
        # child can arrive a page before its parent. Local foreign keys are ON
        # (`db.connect`), so the rows are collected and topologically sorted
        # before anything is written. Threads are small; events, which are
        # not, have no self-reference and stream straight through.
        buffered: list[tuple] = []
        buffered_columns: list[str] = []

        while True:
            body = client.personal_pull(table.name, limit=page, cursor=cursor)
            stats.requests += 1
            columns = list(body.get("columns") or ())
            rows = [tuple(r) for r in body.get("rows") or ()]
            if rows:
                if table.parent is not None:
                    buffered.extend(rows)
                    buffered_columns = columns
                else:
                    _apply(conn, table, columns, rows)
                received += len(rows)
            cursor = body.get("nextCursor")
            entry = {"cursor": cursor, "received": received,
                     "done": cursor is None, "at": _now_ms()}
            tables[table.name] = entry
            if table.parent is None:
                save_state(config_dir, state)
            if cursor is None:
                break

        if buffered:
            _apply(conn, table, buffered_columns,
                   sync.order_by_parent(table, buffered, buffered_columns))
            save_state(config_dir, state)
        if received:
            stats.rows[table.name] = received

    record["pull"]["lastSuccessAt"] = _now_ms()
    save_state(config_dir, state)
    return stats


def _apply(conn: sqlite3.Connection, table: sync.Table, columns: Sequence[str],
           rows: Sequence[Sequence[Any]]) -> None:
    """One page, in one local transaction."""
    if not rows:
        return
    statement = wire_upsert_sql(table, columns)
    conn.execute("BEGIN")
    try:
        conn.executemany(statement, [list(r) for r in rows])
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --------------------------------------------------------------------------
# publish -- the redacted projection, and nothing else
# --------------------------------------------------------------------------


class Unsafe(RemoteError):
    """The projection failed its own audit. Nothing was sent.

    This lives in the transport, not only in the CLI that prints the report,
    because `docs/ACCOUNTS.md` §2's whole argument is that the boundary is
    enforced where the row is written rather than where it is read. A refusal
    a caller can skip by not calling the reporting function is not a refusal.
    """


#: The order `POST /v1/team/publish` requires (§4.1). Out of order is a 409.
PUBLISH_ORDER = ("repos", "sessions", "spans", "withheld")


def _repo_row(r: redact.Repo) -> dict:
    return {"repoId": r.repo_id, "remoteUrl": r.remote_url, "forge": r.forge,
            "owner": r.owner, "repo": r.repo, "webUrl": r.web_url, "name": r.name}


def _session_row(s: redact.Session) -> dict:
    return {"sessionId": s.session_id, "repoId": s.repo_id, "actor": s.actor,
            "source": s.source, "gitBranch": s.git_branch, "startedAt": s.started_at,
            "endedAt": s.ended_at, "activeMs": s.active_ms, "eventCount": s.event_count}


def _span_row(s: redact.Span) -> dict:
    return {"spanId": s.span_id, "sessionId": s.session_id, "threadRole": s.thread_role,
            "startedAt": s.started_at, "endedAt": s.ended_at, "eventCount": s.event_count}


def publication_rows(pub: redact.Publication, host_id: str) -> dict[str, list[dict]]:
    """`redact.Publication` as the four `kind`s the contract accepts.

    Field by field, from the dataclasses, with no `vars()` and no loop over
    attributes. A projection serialized reflectively is a projection that
    starts shipping whatever somebody adds to the dataclass next, which is the
    closed-by-default rule losing to convenience.
    """
    return {
        "repos": [_repo_row(r) for r in pub.repos],
        "sessions": [_session_row(s) for s in pub.sessions],
        "spans": [_span_row(s) for s in pub.spans],
        "withheld": [{"hostId": host_id, "withheldMs": pub.withheld_ms,
                      "withheldProjects": pub.withheld_projects,
                      "publishedMs": pub.published_ms}],
    }


def check_publishable(conn: sqlite3.Connection, pub: redact.Publication) -> redact.Audit:
    """Re-run the audit on the exact projection about to be sent, or refuse.

    `redact.audit` proves the negative it can prove: no full local path, no
    `project_id` -- which IS sha256 of a path -- and no username or hostname
    reaches a published field. An unclassified column is equally fatal: it
    means a migration added something nobody has ruled on, and closed-by-
    default cannot be honoured by a publisher that has not read the ruling.
    """
    unclassified = redact.unclassified(conn)
    if unclassified:
        listed = ", ".join(f"{t}.{c}" for t, c in unclassified[:5])
        raise Unsafe(
            f"{len(unclassified)} schema column(s) have not been classified for "
            f"publication: {listed}.\n"
            "    nothing was sent. Run `cci privacy` and rule on them in "
            "redact.FIELDS first."
        )
    audit = redact.audit(pub, redact.local_secrets(conn))
    if not audit.clean:
        listed = "\n      ".join(audit.leaks[:5])
        raise Unsafe(
            f"the projection failed its own audit with {len(audit.leaks)} leak(s); "
            "nothing was sent.\n"
            f"      {listed}\n"
            "    run `cci privacy` for the full report."
        )
    return audit


def publish(conn: sqlite3.Connection, client: Client, pub: redact.Publication, *,
            host_id: str, batch: int = PUSH_BATCH,
            on_part: Callable[[str, int], None] | None = None) -> dict[str, BatchResult]:
    """Send the redacted projection to the team store.

    The audit runs here, against the object that is about to be serialized,
    and refuses before the first request rather than between two of them. A
    half-published projection is worse than none: `repos` and `sessions` would
    be readable by a team while the `withheld` counters that make the totals
    honest never arrive, which is rule 3 failing silently.
    """
    check_publishable(conn, pub)

    parts = publication_rows(pub, host_id)
    out: dict[str, BatchResult] = {}
    for kind in PUBLISH_ORDER:
        rows = parts[kind]
        totals = BatchResult()
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            got = client.team_publish(kind, pub.actor, chunk)
            totals = BatchResult(totals.received + got.received,
                                 totals.applied + got.applied,
                                 totals.rejected + got.rejected)
        out[kind] = totals
        if on_part is not None:
            on_part(kind, totals.applied)
    return out


def record_publish(config_dir: Path, base_url: str, account_id: str, *,
                   confirmed: bool = False, rows: int = 0) -> None:
    """Remember that this machine has published, and that somebody agreed to it.

    `confirmedAt` is why this is stored at all: `cci publish` is the one
    command that sends a person's data somewhere other people read, so the
    first one on a machine asks. Recording the answer means it asks once
    rather than becoming a prompt people learn to type through.
    """
    state = load_state(config_dir)
    record = _target(state, target_key(base_url, account_id)).setdefault("publish", {})
    record["lastSuccessAt"] = _now_ms()
    record["rows"] = rows
    if confirmed:
        record.setdefault("confirmedAt", _now_ms())
    save_state(config_dir, state)


def has_published(config_dir: Path, base_url: str, account_id: str) -> bool:
    """Whether this (server, account) has ever been confirmed for publication."""
    state = load_state(config_dir)
    record = state.get("targets", {}).get(target_key(base_url, account_id), {})
    return bool(record.get("publish", {}).get("confirmedAt"))


def last_push_at(config_dir: Path, base_url: str, account_id: str) -> int | None:
    """Epoch-ms of the last completed push, for `cci doctor`."""
    state = load_state(config_dir)
    record = state.get("targets", {}).get(target_key(base_url, account_id), {})
    value = record.get("push", {}).get("lastSuccessAt")
    return value if isinstance(value, int) else None
