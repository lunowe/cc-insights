"""The ASGI app: wiring, error handlers, `/healthz`.

Nothing here decides anything about access. The rules live in `scope.py`,
`auth.py` and the two route modules, and this file's only job is to make sure
they are all reachable and that every failure comes out in one shape.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cci_server import config, github
from cci_server.db import Database, now_ms
from cci_server.routes import auth_routes, join_page, personal, team_data, teams


def create_app(
    settings: config.Settings | None = None,
    *,
    db: Database | None = None,
    provider: github.Provider | None = None,
    migrate: bool = True,
) -> FastAPI:
    """Build the app.

    Every collaborator is injectable because the tests need two of them
    replaced -- a scripted GitHub, and a database that is torn down after the
    run. An app that could only be built from the environment would mean the
    device flow's four error branches are untestable, and those are the
    branches a CLI gets wrong.
    """
    settings = settings or config.from_env()
    # Who owns the pool decides who closes it. An app handed a database closes
    # nothing on shutdown: the caller's pool may outlive this app, and a test
    # that stands up two apps against one database would otherwise have the
    # first one's teardown break the second.
    owns_db = db is None
    database = db or Database(settings.database_url)
    if provider is None:
        provider = (
            github.GitHubProvider(settings.github_client_id, settings.github_client_secret)
            if settings.github_configured
            else _Unconfigured()
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # What /healthz reports is the schema's state, not what this boot
        # changed: the image runs `cci-server migrate` before `run`, so the
        # migration `run` does itself applies nothing, and reporting that
        # read as "no migrations" on every healthy deploy.
        if migrate:
            database.migrate()
        app.state.migrations = sorted(_applied(database))
        yield
        if owns_db:
            database.close()

    app = FastAPI(
        title="CC-Insights account server",
        version="0.1.0",
        lifespan=lifespan,
        # The contract is docs/SERVER_API.md, which is frozen and hand-written.
        # A generated schema next to it would be a second description of the
        # same thing, and the two would disagree the first week.
        openapi_url=None,
    )
    app.state.db = database
    app.state.settings = settings
    app.state.provider = provider
    app.state.migrations = []

    app.include_router(auth_routes.router)
    app.include_router(personal.router)
    app.include_router(teams.router)
    app.include_router(team_data.router)
    app.include_router(join_page.router)

    @app.get("/healthz")
    def healthz():
        if not database.healthy():
            # 503 rather than 500 so a load balancer takes the instance out
            # instead of serving errors from it. The store holds filesystem
            # paths; a half-working instance is not one to keep in rotation.
            return JSONResponse(
                status_code=503,
                content={"error": "database_unavailable",
                         "message": "The database is not reachable."},
            )
        return {"status": "ok", "migrations": app.state.migrations, "now": now_ms()}

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        # `ApiError` already puts the envelope in `detail`. A bare
        # `HTTPException` from FastAPI's own machinery does not, and gets one
        # here so a client never has to parse two shapes.
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": _code_for(exc.status_code), "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=body,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # 400, not FastAPI's default 422. The contract lists 400 for "malformed
        # body", and a client branching on status codes should not have to
        # learn a second one for the same condition.
        return JSONResponse(
            status_code=400,
            content={"error": "malformed_request",
                     "message": f"The request body or query is not valid: {exc.errors()}"},
        )

    return app


def _applied(database: Database) -> list[int]:
    with database.connection() as conn:
        from cci_server.db import applied_versions

        return list(applied_versions(conn))


_CODES = {400: "bad_request", 401: "unauthenticated", 403: "forbidden",
          404: "not_found", 405: "method_not_allowed", 409: "conflict",
          413: "batch_too_large", 429: "rate_limited", 500: "internal_error"}


def _code_for(status: int) -> str:
    return _CODES.get(status, "error")


class _Unconfigured:
    """Stands in when no GitHub app is configured.

    Fails loudly on the first sign-in attempt rather than at import, because
    an instance with no GitHub credentials is still a working server for every
    account that already has a token -- and refusing to start would turn a
    misconfigured secret into a total outage instead of a broken login.
    """

    host = "github.com"

    def _fail(self, *_a, **_k):
        raise github.UpstreamError(
            "No GitHub OAuth app is configured on this server "
            "(CCI_SERVER_GITHUB_CLIENT_ID / _SECRET)."
        )

    start_device = poll_token = identify = accessible_repos = _fail
