"""Credential storage, separate from the config people copy around.

`load` returns the stored identity unchanged. `token_for` layers an explicit
token over CC_INSIGHTS_TOKEN over that stored token, as sync_url_for does for
the sync URL. An override is not evidence of a different account's identity;
only the server can establish that. Nothing here makes a network request.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cc_insights import config


@dataclass(frozen=True, slots=True)
class Credential:
    token: str = field(repr=False)
    account_id: str
    server_url: str
    issued_at: int  # Epoch-milliseconds, like the timestamps in the database.


def _path(config_dir: Path | None) -> Path:
    return (config_dir or config.default_config_dir()).expanduser() / "credentials.toml"


def load(config_dir: Path | None = None) -> Credential | None:
    """Return the stored credential, or None when it cannot be read."""
    try:
        with _path(config_dir).open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, ValueError):
        return None

    # Valid TOML can still be an incomplete credential. Treating a missing
    # account id or a string timestamp as signed in only moves the crash to
    # the next caller, where the broken file is much harder to diagnose.
    if any(not isinstance(raw.get(key), str) or not raw[key]
           for key in ("token", "account_id", "server_url")):
        return None
    if type(raw.get("issued_at")) is not int or raw["issued_at"] < 0:
        return None
    return Credential(raw["token"], raw["account_id"], raw["server_url"], raw["issued_at"])


def save(credential: Credential, config_dir: Path | None = None) -> Path:
    """Persist a credential without ever creating a world-readable token."""
    path = _path(config_dir)
    text = (
        f"token = {config._toml_str(credential.token)}\n"
        f"account_id = {config._toml_str(credential.account_id)}\n"
        f"server_url = {config._toml_str(credential.server_url)}\n"
        f"issued_at = {credential.issued_at}\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # config._atomic_write uses Path.write_text, whose creation permissions
    # depend on the umask. chmod afterwards leaves a window in which the token
    # is readable by others. mkstemp creates 0600 before any secret is written;
    # a unique name also keeps concurrent saves from sharing a temporary file.
    fd, name = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def clear(config_dir: Path | None = None) -> None:
    """Remove the stored credential; an environment override remains in effect."""
    _path(config_dir).unlink(missing_ok=True)


def token_for(credential: Credential | None, override: str | None = None) -> str | None:
    """Resolve a token without persisting an environment or explicit override."""
    return override or os.environ.get("CC_INSIGHTS_TOKEN") or (
        credential.token if credential is not None else None
    )


def is_signed_in(config_dir: Path | None = None, *, override: str | None = None) -> bool:
    """Whether a token is available locally, not whether the server accepts it."""
    return token_for(load(config_dir), override) is not None
