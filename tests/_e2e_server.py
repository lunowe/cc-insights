"""Run the real account server, with only GitHub scripted. Not a test module.

Executed by `tests/server_harness.py` inside `server/.venv`, which is a
different interpreter from the one running the client tests -- the server
needs FastAPI, uvicorn and psycopg and the client is required to need
nothing. Hence a subprocess and a real socket rather than an import.

**Everything here is the shipped server.** `create_app` is the production
factory, the database is a real PostgreSQL, the migrations are the real
migrations, and the routes are the real routes. The single substitution is
`github.FakeProvider`, and it is not a shortcut around testing the flow: four
of the device grant's five exits are states GitHub will not produce on
request, so the alternative to scripting them is not testing them.

The `/test/*` control routes are mounted here, in the harness, and not in
`server/` -- which is off limits and, more to the point, must not grow an
endpoint that approves a sign-in.
"""

from __future__ import annotations

import argparse
import sys

from fastapi import APIRouter
from pydantic import BaseModel

from cci_server import config, github
from cci_server.app import create_app
from cci_server.repoid import github_remote, repo_id


class Identity(BaseModel):
    token: str
    subject: str
    login: str
    repos: list[str] = []


class Script(BaseModel):
    device: str
    #: Each entry is {"error": "..."} or {"accessToken": "..."}, consumed in
    #: order; the last one repeats, so a test says "pending twice then granted"
    #: without knowing how many times the client will poll.
    results: list[dict]


def control_router(provider: github.FakeProvider) -> APIRouter:
    router = APIRouter(prefix="/test", tags=["harness"])

    @router.post("/provider/identity")
    def identity(body: Identity):
        provider.identities[body.token] = github.ForgeIdentity(
            subject=body.subject, login=body.login
        )
        provider.repos[body.token] = [
            repo_id(github_remote(provider.host, full)) for full in body.repos
        ]
        return {"ok": True}

    @router.post("/provider/script")
    def script(body: Script):
        provider.script[body.device] = [
            github.TokenResult(access_token=r.get("accessToken"), error=r.get("error"))
            for r in body.results
        ]
        return {"ok": True, "device": body.device}

    @router.get("/provider/devices")
    def devices():
        return {"devices": sorted(provider.script)}

    return router


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--device-ttl", type=int, default=900)
    args = parser.parse_args(argv)

    settings = config.from_env({
        "CCI_SERVER_DATABASE_URL": args.database_url,
        # Credentials are present so `github_configured` is true and the real
        # routing is exercised; the provider below is what actually answers.
        "CCI_SERVER_GITHUB_CLIENT_ID": "harness-client",
        "CCI_SERVER_GITHUB_CLIENT_SECRET": "harness-secret",
        "CCI_SERVER_GITHUB_SCOPE": "read:user repo",
        "CCI_SERVER_DEVICE_FLOW_TTL_S": str(args.device_ttl),
    })
    # A one-second interval, because the tests must actually wait it out. The
    # server's throttle is real and measured on its own clock, so a test that
    # fakes `sleep` never gets past `slow_down` -- which is the correct
    # behaviour and would simply hang. GitHub's own interval is 5; the number
    # is the provider's to report and nothing in the client assumes it.
    provider = github.FakeProvider(host="github.com", interval=1)
    app = create_app(settings, provider=provider, migrate=True)
    app.include_router(control_router(provider))

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
