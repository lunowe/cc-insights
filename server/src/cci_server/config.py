"""Settings, from the environment, resolved once at import of `app`.

Nothing here reads a file. The client has `config.toml` because a person edits
it by hand; a server is configured by whatever started it, and a second place
to look is a second place for the answer to be stale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Above this, a push is refused rather than truncated. 5000 rows of `event`
#: is roughly 1 MB of JSON, and the measured corpus of 191,475 rows is ~40
#: requests for the largest table -- which is the point of the cap being this
#: high. Truncating instead of refusing would make a client that ignores the
#: response silently lose the tail of every batch.
MAX_BATCH_ROWS = 5000

#: Page size for pulls and for every paginated read.
DEFAULT_PAGE = 1000
MAX_PAGE = 5000


@dataclass(frozen=True)
class Settings:
    database_url: str
    github_client_id: str
    github_client_secret: str
    #: `read:user` alone verifies identity. Adding `repo` is what makes
    #: docs/SERVER_API.md §4.6 able to derive scope from real repo access --
    #: and it is opt-in per deployment, because it is a broad grant to ask for
    #: and an instance running on team rosters alone works without it.
    github_scope: str
    #: How long a device flow lives if nobody approves it. GitHub's own is 900s
    #: and ours must not outlive it, or a client polls a code GitHub has
    #: already forgotten and gets `expired_token` from us anyway, later.
    device_flow_ttl_s: int
    #: Revoked tokens stay listable this long. Somebody checking whether a lost
    #: laptop's token is dead needs to see that it IS dead, not see nothing and
    #: wonder whether they clicked the button.
    revoked_token_retention_ms: int

    @property
    def github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)


def from_env(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    url = e.get("CCI_SERVER_DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "CCI_SERVER_DATABASE_URL is not set. The server has no default: a "
            "default would be a database somebody did not choose, and this one "
            "holds filesystem paths."
        )
    return Settings(
        database_url=url,
        github_client_id=e.get("CCI_SERVER_GITHUB_CLIENT_ID", ""),
        github_client_secret=e.get("CCI_SERVER_GITHUB_CLIENT_SECRET", ""),
        github_scope=e.get("CCI_SERVER_GITHUB_SCOPE", "read:user"),
        device_flow_ttl_s=int(e.get("CCI_SERVER_DEVICE_FLOW_TTL_S", "900")),
        revoked_token_retention_ms=int(
            e.get("CCI_SERVER_REVOKED_TOKEN_RETENTION_MS", str(30 * 24 * 3600 * 1000))
        ),
    )
