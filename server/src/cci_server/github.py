"""The GitHub half of the device authorisation grant.

docs/ACCOUNTS.md §6 chose the device grant over a redirect because a CLI cannot
reliably receive a browser callback, and a local listener on a random port is
the fragile version of that. It also works identically over SSH, which is the
case that actually matters: the second machine is usually not the one in front
of you.

Everything GitHub-shaped is behind `Provider` so the routes never touch httpx
and the tests never touch the network. `FakeProvider` is not a shortcut around
testing the flow -- it is what makes the four error paths
(`authorization_pending`, `slow_down`, `expired_token`, `access_denied`)
testable at all, since GitHub will not produce them on demand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from cci_server.repoid import github_remote, repo_id

GITHUB = "github"

_DEVICE_CODE_URL = "https://github.com/login/device/code"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_API = "https://api.github.com"

#: The four codes RFC 8628 §3.5 defines and GitHub actually sends. Anything
#: else from GitHub is a bug on one side or the other and must not be
#: translated into "keep polling" -- a client that retries forever on an
#: unknown error is a client that never tells its user what went wrong.
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
EXPIRED = "expired_token"
DENIED = "access_denied"
KNOWN_ERRORS = frozenset({PENDING, SLOW_DOWN, EXPIRED, DENIED})


@dataclass(frozen=True)
class DeviceStart:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenResult:
    """Exactly one of `access_token` or `error` is set."""

    access_token: str | None = None
    error: str | None = None
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ForgeIdentity:
    #: GitHub's NUMERIC id. Never the login: logins are renameable and
    #: reusable, so keying an account on one hands it to whoever claims the
    #: name next.
    subject: str
    login: str


class Provider(Protocol):
    host: str

    def start_device(self, scope: str) -> DeviceStart: ...
    def poll_token(self, device_code: str) -> TokenResult: ...
    def identify(self, access_token: str) -> ForgeIdentity: ...
    def accessible_repo_ids(self, access_token: str) -> list[str]: ...


class UpstreamError(RuntimeError):
    """GitHub could not be reached, or answered with something unusable.

    Distinct from every device-flow error code, because this one is not the
    client's problem and is usually transient: it becomes a 502, not a 400.
    """


class GitHubProvider:
    """The real one."""

    host = "github.com"

    def __init__(self, client_id: str, client_secret: str, *, timeout_s: float = 10.0) -> None:
        self._id = client_id
        self._secret = client_secret
        self._timeout = timeout_s

    def _post(self, url: str, data: dict) -> dict:
        try:
            r = httpx.post(
                url, data=data, headers={"Accept": "application/json"}, timeout=self._timeout
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # pragma: no cover - network
            raise UpstreamError(str(exc)) from exc

    def start_device(self, scope: str) -> DeviceStart:
        body = self._post(_DEVICE_CODE_URL, {"client_id": self._id, "scope": scope})
        if "device_code" not in body:
            raise UpstreamError(f"no device_code in response: {sorted(body)}")
        return DeviceStart(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body.get("verification_uri", "https://github.com/login/device"),
            expires_in=int(body.get("expires_in", 900)),
            interval=int(body.get("interval", 5)),
        )

    def poll_token(self, device_code: str) -> TokenResult:
        body = self._post(
            _TOKEN_URL,
            {
                "client_id": self._id,
                "client_secret": self._secret,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if token := body.get("access_token"):
            scopes = tuple(s for s in (body.get("scope") or "").split(",") if s)
            return TokenResult(access_token=token, scopes=scopes)
        error = body.get("error") or "invalid_request"
        return TokenResult(error=error)

    def _get(self, path: str, access_token: str, params: dict | None = None) -> httpx.Response:
        try:
            r = httpx.get(
                f"{_API}{path}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {access_token}",
                },
                params=params,
                timeout=self._timeout,
            )
            r.raise_for_status()
            return r
        except Exception as exc:  # pragma: no cover - network
            raise UpstreamError(str(exc)) from exc

    def identify(self, access_token: str) -> ForgeIdentity:
        body = self._get("/user", access_token).json()
        return ForgeIdentity(subject=str(body["id"]), login=body["login"])

    def accessible_repo_ids(self, access_token: str) -> list[str]:
        """Every repo this token can reach, as `repo_id`s.

        docs/SERVER_API.md §4.6: this runs once, at sign-in, and the token is
        discarded in the same request. Access is therefore only as fresh as the
        last `cci login` -- stated in the contract rather than pretended away,
        because docs/REDACTION.md §5 already admits revocation is not
        retroactive and this is the same honesty applied to a lag.

        Failure is not fatal. A token whose scopes do not cover repositories,
        or a GitHub that is having a bad minute, must not stop somebody signing
        in: team rosters are the primary mechanism and this layer is additive.
        """
        out: list[str] = []
        page = 1
        while page <= 10:  # 1000 repos. Past that, rosters are the answer.
            try:
                r = self._get(
                    "/user/repos",
                    access_token,
                    {"per_page": 100, "page": page, "affiliation":
                     "owner,collaborator,organization_member"},
                )
            except UpstreamError:
                break
            rows = r.json()
            if not rows:
                break
            for row in rows:
                full = row.get("full_name")
                if full:
                    out.append(repo_id(github_remote(self.host, full)))
            if len(rows) < 100:
                break
            page += 1
        return out


@dataclass
class FakeProvider:
    """A scripted GitHub, for the tests.

    Every device-flow branch worth testing is a branch GitHub will not produce
    on request, so the alternative to this class is not "test it for real" --
    it is "do not test it".
    """

    host: str = "github.test"
    #: device_code -> the sequence of results `poll_token` returns, in order.
    #: The last entry repeats, so a test can say "pending twice then granted"
    #: without knowing how many times the code under test will poll.
    script: dict[str, list[TokenResult]] = field(default_factory=dict)
    identities: dict[str, ForgeIdentity] = field(default_factory=dict)
    repos: dict[str, list[str]] = field(default_factory=dict)
    interval: int = 5
    expires_in: int = 900
    started: list[str] = field(default_factory=list)
    _n: int = 0

    def start_device(self, scope: str) -> DeviceStart:
        self._n += 1
        code = f"gh-device-{self._n}"
        self.started.append(scope)
        self.script.setdefault(code, [TokenResult(error=PENDING)])
        return DeviceStart(
            device_code=code,
            user_code=f"CODE-{self._n:04d}",
            verification_uri=f"https://{self.host}/login/device",
            expires_in=self.expires_in,
            interval=self.interval,
        )

    def poll_token(self, device_code: str) -> TokenResult:
        seq = self.script.get(device_code)
        if not seq:
            return TokenResult(error=EXPIRED)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def identify(self, access_token: str) -> ForgeIdentity:
        if access_token not in self.identities:
            raise UpstreamError(f"no scripted identity for {access_token!r}")
        return self.identities[access_token]

    def accessible_repo_ids(self, access_token: str) -> list[str]:
        return list(self.repos.get(access_token, ()))
