"""`repo_id`, recomputed here exactly as `redact.repo_id` computes it.

Two programs have to agree on this value or the team store does not work: the
laptop derives it from a git remote, the server derives it from the GitHub API,
and a session published by the first is only visible through the second if the
two land on the same string.

So this is a deliberate duplication of four lines from `cc_insights.ids` and
`cc_insights.redact`, and `test_repo_id_matches_the_client` imports the real
ones and asserts they agree. Importing them instead was the alternative and it
is worse: the server would depend on the client package, whose zero-dependency
install is a tested feature, to reuse a `hashlib` call.

Hashed for a stable, fixed-width key and NOT for secrecy. The input -- the
credential-stripped remote -- is already public on the far side of the
boundary, so the confirmation attack of docs/REDACTION.md §0 has nothing to
confirm. That is the whole difference between this and `project_id`.
"""

from __future__ import annotations

import hashlib

_SEP = "\x1f"  # ASCII unit separator: cannot occur in a URL
_ID_LEN = 32


def make_id(*parts: str | None) -> str:
    joined = _SEP.join("\x00" if p is None else p for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_ID_LEN]


def repo_id(normalized_remote: str) -> str:
    return make_id("repo", normalized_remote)


def github_remote(host: str, full_name: str) -> str:
    """The normalized remote for `owner/repo` on a GitHub host.

    `grouping.normalize_remote` turns every spelling of a GitHub remote --
    scp-style `git@github.com:owner/repo.git`, `ssh://`, `https://` with a
    trailing `.git` -- into exactly this: https, lowercased host, no `.git`, no
    credentials. Constructing it rather than normalizing a URL from the API
    means there is one fewer parser to disagree with the client's.
    """
    return f"https://{host.lower()}/{full_name}"
