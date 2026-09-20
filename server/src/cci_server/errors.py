"""One error body for the whole API, and the rules about which code to use.

docs/SERVER_API.md §0: every non-2xx response is

    {"error": "<machine_code>", "message": "<a sentence for a human>"}

`error` is stable and may be branched on; `message` is prose and may change.
Never a 200 with an error in it -- the local API froze that rule first
(docs/API.md § Errors) and there is no reason for the two to differ.
"""

from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    """An error with a machine-readable code.

    Subclasses `HTTPException` so FastAPI's own raises -- 422 on a malformed
    body, for instance -- travel the same path; `app.py` installs handlers that
    give those the same envelope rather than letting two shapes exist. A client
    that has to branch on two error formats will get one of them wrong.
    """

    def __init__(self, status: int, code: str, message: str,
                 *, headers: dict[str, str] | None = None, **extra: object) -> None:
        body: dict[str, object] = {"error": code, "message": message}
        body.update(extra)
        # Headers travel on the exception, not on an injected `Response`.
        # FastAPI only merges that one on a normal return, so a `Retry-After`
        # set there is silently dropped on exactly the responses that need it.
        super().__init__(status_code=status, detail=body, headers=headers)
        self.code = code
        self.extra = extra


def bad_request(code: str, message: str, **extra: object) -> ApiError:
    return ApiError(400, code, message, **extra)


def unauthenticated(message: str = "A valid bearer token is required.") -> ApiError:
    return ApiError(401, "unauthenticated", message)


def forbidden(code: str, message: str) -> ApiError:
    """403 -- authenticated, allowed to know the thing exists, not allowed to act.

    Use this ONLY when the caller already knows the resource exists: a member
    reaching for an admin route on their own team, say. Everything else is
    `not_found`, because a 403 that is distinguishable from a 404 answers the
    question "does this exist" for a caller who was not entitled to ask it --
    the same leak docs/ACCOUNTS.md §5 rule 1 forbids in an aggregate.
    """
    return ApiError(403, code, message)


def not_found(message: str = "No such resource.") -> ApiError:
    """404 -- absent, OR present and outside your scope, indistinguishably."""
    return ApiError(404, "not_found", message)


def conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


def too_large(message: str, **extra: object) -> ApiError:
    return ApiError(413, "batch_too_large", message, **extra)
