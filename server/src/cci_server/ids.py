"""Ids, and the two kinds this server mints.

The client's `ids.make_id` hashes content, which is what makes a row pushed
twice collapse instead of duplicating. That property is load-bearing for
everything that crosses this API and it is NOT reimplemented here -- every id
in a push or a publish arrives already computed by the laptop, and the server
never recomputes one. Rehashing on arrival would mean two implementations of
the identity rule, and the day they disagreed the same work would exist twice.

What the server does mint is its own opaque handles -- accounts, teams, tokens
-- which have no content to hash and are random.
"""

from __future__ import annotations

import secrets

#: Prefixes, so an id in a log or a bug report says what it is without a
#: lookup, and so a token pasted into the wrong field fails loudly.
ACCOUNT = "acc"
IDENTITY = "idt"
TEAM = "tm"
TOKEN = "tok"
DEVICE = "dev"


def new_id(prefix: str) -> str:
    """A random, prefixed, URL-safe id.

    128 bits. These are not secrets -- they appear in URLs and in responses --
    so the width is about collision, not guessing, and 128 bits is past the
    point where a birthday collision is worth reasoning about.
    """
    return f"{prefix}_{secrets.token_urlsafe(16)}"
