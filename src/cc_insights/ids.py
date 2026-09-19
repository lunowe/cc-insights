"""Deterministic id generation.

Every id in CC-Insights is a hash of its natural key, never a counter. This is
what makes ingest idempotent (re-running over the same logs is a no-op) and
what will let several machines push into one database without collisions --
the same logical row computes the same id everywhere.

Never change these functions without a migration: the ids are the data.
"""

from __future__ import annotations

import hashlib

ID_LEN = 32
_SEP = "\x1f"  # ASCII unit separator: cannot occur in the paths/uuids we hash


def make_id(*parts: str | None) -> str:
    """Stable id from a natural key. None and "" are distinct from each other."""
    joined = _SEP.join("\x00" if p is None else p for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:ID_LEN]


def session_id(host_id: str, source: str, native_id: str) -> str:
    return make_id(host_id, source, native_id)


def thread_id(session_id_: str, native_id: str) -> str:
    return make_id(session_id_, native_id)


def event_id(session_id_: str, native_event_id: str) -> str:
    return make_id(session_id_, native_event_id)


def span_id(thread_id_: str, started_at: int) -> str:
    return make_id(thread_id_, str(started_at))


def project_id(root_path: str) -> str:
    return make_id(root_path)


def content_fallback_id(raw_line: str) -> str:
    """native_event_id for events whose source gives no stable identifier."""
    return hashlib.sha256(raw_line.encode("utf-8", "replace")).hexdigest()[:ID_LEN]
