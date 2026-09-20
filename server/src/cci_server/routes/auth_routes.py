"""Sign-in: the device authorisation grant, and token management.

docs/SERVER_API.md §2. The shape of this file is dictated by one rule from
docs/ACCOUNTS.md §6 -- the client never holds a GitHub credential -- and by
one operational fact: polling too fast gets the whole instance rate-limited at
GitHub, and then nobody can sign in at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from cci_server import device, github, tokens
from cci_server.auth import get_conn, principal
from cci_server.errors import ApiError, not_found

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class DeviceStartBody(BaseModel):
    clientName: str | None = Field(default=None, max_length=200)


class DeviceTokenBody(BaseModel):
    deviceCode: str


@router.post("/device/start")
def device_start(request: Request, body: DeviceStartBody | None = None, conn=Depends(get_conn)):
    provider: github.Provider = request.app.state.provider
    settings = request.app.state.settings
    try:
        return device.start(
            conn, provider, scope=settings.github_scope,
            client_name=(body.clientName if body else None),
            ttl_s=settings.device_flow_ttl_s,
        )
    except github.UpstreamError as exc:
        # 502 and not 500: GitHub being unreachable is not this server's bug
        # and not the client's, and it is usually over in a minute. A client
        # that sees 500 tells its user to file a report; one that sees 502
        # tells them to try again.
        raise ApiError(502, "upstream_unavailable",
                       f"GitHub could not be reached: {exc}") from exc


@router.post("/device/token")
def device_token(request: Request, body: DeviceTokenBody, conn=Depends(get_conn)):
    provider: github.Provider = request.app.state.provider
    settings = request.app.state.settings
    outcome = device.poll(conn, provider, body.deviceCode, settings=settings)
    if outcome.error is None:
        return outcome.token_response

    extra: dict = {}
    headers = None
    if outcome.retry_after_s is not None:
        # A header AND a body field, from one value so they cannot disagree.
        # The header is for a client that only speaks HTTP; the body field is
        # for one that reads `interval`. Both are present only on the two
        # retryable codes, which is how a client tells "wait" from "stop"
        # without a lookup table.
        extra = {"interval": outcome.retry_after_s, "retryAfter": outcome.retry_after_s}
        headers = {"Retry-After": str(outcome.retry_after_s)}
    raise ApiError(outcome.status, outcome.error, outcome.message,
                   headers=headers, **extra)


@router.get("/whoami")
def whoami(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    account = conn.execute(
        "SELECT account_id, actor, created_at FROM account WHERE account_id = %s",
        (who.account_id,),
    ).fetchone()
    identities = conn.execute(
        """SELECT provider, subject, label, created_at
           FROM identity WHERE account_id = %s ORDER BY created_at""",
        (who.account_id,),
    ).fetchall()
    teams = conn.execute(
        """SELECT t.team_id, t.name, tm.role
           FROM team_member tm JOIN team t ON t.team_id = tm.team_id
           WHERE tm.account_id = %s ORDER BY t.name""",
        (who.account_id,),
    ).fetchall()
    tok = conn.execute(
        """SELECT token_id, name, created_at, last_used_at, expires_at
           FROM api_token WHERE token_id = %s""",
        (who.token_id,),
    ).fetchone()
    return {
        "accountId": account["account_id"],
        "actor": account["actor"],
        "createdAt": account["created_at"],
        # An array today, when only GitHub ships, because that is the whole
        # reason `identity` is a table: one account, several ways to sign in,
        # and no client change on the day email arrives.
        "identities": [
            {"provider": i["provider"], "subject": i["subject"],
             "label": i["label"], "createdAt": i["created_at"]}
            for i in identities
        ],
        "teams": [
            {"teamId": t["team_id"], "name": t["name"], "role": t["role"]} for t in teams
        ],
        "token": {
            "tokenId": tok["token_id"], "name": tok["name"],
            "createdAt": tok["created_at"], "lastUsedAt": tok["last_used_at"],
            "expiresAt": tok["expires_at"],
        },
    }


@router.get("/tokens")
def list_tokens(request: Request, who: tokens.Principal = Depends(principal),
                conn=Depends(get_conn)):
    retention = request.app.state.settings.revoked_token_retention_ms
    return {"tokens": tokens.listing(conn, who.account_id, retention_ms=retention)}


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: str, who: tokens.Principal = Depends(principal),
                 conn=Depends(get_conn)):
    if not tokens.revoke(conn, who.account_id, token_id):
        # 404 rather than 403 for a token on another account. A distinguishable
        # 403 would confirm that the id exists, which is the one thing someone
        # enumerating ids wants to learn.
        raise not_found("No such token on this account.")
    return Response(status_code=204)


@router.post("/logout", status_code=204)
def logout(who: tokens.Principal = Depends(principal), conn=Depends(get_conn)):
    """Revoke the token presenting this request.

    Separate from `DELETE /tokens/{id}` because it is the one revocation a
    client can perform without knowing its own id, and making `cci logout` do
    a round trip to learn that first would be one more thing to fail on the
    machine somebody is trying to sign out of.
    """
    tokens.revoke(conn, who.account_id, who.token_id)
    return Response(status_code=204)
