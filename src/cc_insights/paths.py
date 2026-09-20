"""Path reasoning that does not assume the host OS.

Every function here takes the *flavor* from the path string itself, never from
`os.name`. That is not pedantry about Windows: the point of v2 is that one
database holds rows from several machines, so a Mac will be asked to reason
about `C:\\Users\\you\\Coding\\repo` and a Windows box about `/Users/me/...`.
`os.path` answers those questions with whatever the reader's OS believes, which
is silently wrong in exactly the case the feature exists for. Passing a Windows
path to `os.path.basename` on a Mac returns the whole string; grouping then
files every Windows project under one meaningless group and the dashboard looks
authoritative doing it.

Nothing here touches the disk, and nothing here rewrites a stored `root_path`.
`project_id = hash(root_path)` is immutable identity: for every POSIX path,
`normalize` returns exactly what `os.path.normpath` returned before this module
existed, and `key` returns exactly what `grouping._norm_dir` returned. Those
equivalences are asserted in tests/test_paths.py, because breaking one forks
the entire history into a duplicate set of rows.

Case is part of the flavor. Windows paths compare case-insensitively (the
filesystem does), POSIX paths do not. Stored paths keep their original case
either way -- only comparison folds.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from collections.abc import Sequence

WINDOWS = "windows"
POSIX = "posix"

#: The separator each flavor is rendered with when we rebuild a path.
SEP = {POSIX: "/", WINDOWS: "\\"}

# `C:`, `c:/`, `Z:\` -- ntpath.splitdrive's notion of a drive. A single letter
# followed by a colon is not a plausible first segment of a POSIX path.
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

_SPLIT_RE = {POSIX: re.compile(r"/"), WINDOWS: re.compile(r"[\\/]")}

#: The flavor of the machine this process is on -- the one question here that
#: is legitimately about the host. Two callers need it: the filesystem probe
#: (a path of any other flavor describes a disk we cannot see) and `config`,
#: picking where this platform keeps its files. Everything else must take the
#: flavor from the path. Tests patch this to stand on the other platform,
#: which is why it is a module attribute and not an inline `os.name` check.
LOCAL = WINDOWS if os.name == "nt" else POSIX


def flavor(path: str) -> str:
    """Which OS's rules this path follows, judged from the string alone.

    An absolute POSIX path is decided before the backslash test, so a Linux
    file that genuinely contains a ``\\`` in its name stays POSIX. That
    ordering is what makes this safe to apply to the existing corpus.
    """
    if not isinstance(path, str) or not path:
        return POSIX
    if path.startswith("\\\\"):          # UNC: \\server\share
        return WINDOWS
    if _DRIVE_RE.match(path):
        return WINDOWS
    if path.startswith("/"):
        return POSIX
    if "\\" in path:
        return WINDOWS
    return POSIX


def _mod(flav: str):
    return ntpath if flav == WINDOWS else posixpath


def normalize(path: str) -> str:
    """Lexical normalization in the path's own flavor. Never hits the disk."""
    if not path:
        return path
    return _mod(flavor(path)).normpath(path)


def split(path: str) -> tuple[str, list[str]]:
    """``(anchor, segments)``.

    The anchor is everything that is not a segment -- ``"/"``, ``"C:\\"``,
    ``"\\\\server\\share\\"``, or ``""`` for a relative path. Splitting this
    way rather than on a separator is what lets a UNC share and a drive letter
    behave like the roots they are.
    """
    flav = flavor(path)
    norm = normalize(path)
    drive, rest = _mod(flav).splitdrive(norm)
    absolute = bool(rest) and rest[0] in SEP[flav] + "/"
    anchor = drive + (SEP[flav] if absolute else "")
    return anchor, [s for s in _SPLIT_RE[flav].split(rest) if s]


def join(anchor: str, segments: Sequence[str], *, flav: str = POSIX) -> str:
    """Rebuild a path from `split`'s pieces, in `flav`'s separator."""
    sep = SEP[flav]
    body = sep.join(segments)
    if not anchor:
        return body or "."
    if anchor.endswith(("\\", "/", ":")):
        return anchor + body
    return anchor + sep + body if body else anchor


def basename(path: str) -> str:
    """Last segment, or the anchor when there is none. Never empty for a path."""
    anchor, segs = split(path)
    return segs[-1] if segs else (anchor or path)


def dirname(path: str) -> str:
    anchor, segs = split(path)
    return join(anchor, segs[:-1], flav=flavor(path))


def key(path: str) -> str:
    """Canonical comparison form: normalized, forward slashes, folded on Windows.

    Use this for dict keys and equality, never for display or storage. A
    Windows key always carries a drive or a leading ``//``, so it cannot
    collide with a POSIX one.
    """
    flav = flavor(path)
    anchor, segs = split(path)
    text = join(anchor, segs, flav=flav).replace("\\", "/")
    return text.casefold() if flav == WINDOWS else text


def _fold(flav: str, segs: Sequence[str]) -> list[str]:
    return [s.casefold() for s in segs] if flav == WINDOWS else list(segs)


def is_root(path: str) -> bool:
    """A filesystem root: ``/``, ``C:\\``, ``\\\\server\\share``."""
    anchor, segs = split(path)
    return bool(anchor) and not segs


def is_ancestor(parent: str, child: str) -> bool:
    """Is `parent` a strict, segment-aligned ancestor of `child`?

    Segment comparison rather than a string prefix, so ``/a/bc`` is not an
    ancestor of ``/a/bcd``. Paths of different flavors never relate.
    """
    flav = flavor(parent)
    if flav != flavor(child):
        return False
    p_anchor, p_segs = split(parent)
    c_anchor, c_segs = split(child)
    if key(p_anchor or ".") != key(c_anchor or "."):
        return False
    if len(p_segs) >= len(c_segs):
        return False
    return _fold(flav, p_segs) == _fold(flav, c_segs[: len(p_segs)])


def same(a: str, b: str) -> bool:
    return key(a) == key(b)


def is_home_like(path: str) -> bool:
    """A user's home directory, or the directory that holds all of them.

    `grouping.anchorable` can ask the live OS where ``$HOME`` is, but only for
    the machine it is running on. A path pushed from another host has a home
    directory this process cannot look up, and letting it anchor rule 4 files
    every unrelated repository on that machine under one group -- the exact
    failure `anchorable` was written to prevent, just arriving over the wire.

    So the well-known layouts are recognized structurally: ``/Users/<name>``,
    ``/home/<name>``, ``/root``, ``C:\\Users\\<name>``, and the parent of each.
    This is a shape test, not a lookup; it is deliberately conservative, and
    anything deeper (``/Users/me/Coding``) still anchors normally.
    """
    flav = flavor(path)
    anchor, segs = split(path)
    if not anchor:
        return False
    low = [s.casefold() for s in segs]
    if len(low) > 2:
        return False
    if flav == WINDOWS:
        return low[:1] == ["users"]
    if low == ["root"]:
        return True
    return low[:1] in (["users"], ["home"])


def abbreviate_home(path: str, home: str) -> str:
    """``/Users/me/Coding/x`` -> ``~/Coding/x``. Display only."""
    if same(path, home):
        return "~"
    if is_ancestor(home, path):
        flav = flavor(path)
        _, h_segs = split(home)
        _, p_segs = split(path)
        return "~" + SEP[flav] + SEP[flav].join(p_segs[len(h_segs) :])
    return path
