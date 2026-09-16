"""Which build of this package is actually running.

`__version__` answers "which release", and it is bumped by hand. It cannot
answer "which code", and on this package those are routinely different
things, because the package reaches a machine two ways with opposite update
semantics:

  * an **editable install** -- `pipx install -e`, a `.pth` pointing at a
    working tree. It picks up every source edit the instant it is saved.
  * a **frozen zipapp** -- `install-service` stages a copy of the package
    and zips it, and launchd runs that file. It changes only when something
    rebuilds it.

So a developer can add a route, watch their CLI call it immediately, and be
answered 404 by a daemon that has been running a pre-feature archive for
days. Both sides report the same `__version__` and the same `CONTRACT`, so
the contract gate passes and `status` prints a confident "running --
webcompanion v1.0.0" for a daemon that predates the feature being asked
for. The only symptom is a bare 404 from a route that demonstrably exists in
the source, which reads as a bug in the route rather than a stale process.

This module gives the two sides something to disagree about out loud: a
content hash of the packaged python sources, baked into the zipapp at build
time and recomputed live everywhere else.

Only `.py` files are hashed. Static assets churn constantly and are served
rather than executed, so folding them in would report skew on every
unrelated edit and teach the user to ignore the one warning that matters.
"""
from __future__ import annotations

import hashlib
from importlib.resources import as_file, files
from pathlib import Path

#: Written into the staged package by `build_zipapp`, read back at runtime.
STAMP_NAME = "_build_id.txt"

#: Long enough that a collision is not a thing anyone will meet, short
#: enough to read out over a screen share.
_DIGEST_CHARS = 16


def compute(pkg_dir: Path) -> str:
    """Hash every `.py` under `pkg_dir`, path and content, order-independent.

    The path goes into the digest as well as the body so that moving code
    between modules -- which changes what imports resolve to -- is a
    different build even when the bytes are identical in aggregate.
    """
    pkg_dir = Path(pkg_dir)
    h = hashlib.sha256()
    for path in sorted(pkg_dir.rglob("*.py"), key=lambda p: p.relative_to(pkg_dir).as_posix()):
        if "__pycache__" in path.parts:
            continue
        h.update(path.relative_to(pkg_dir).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:_DIGEST_CHARS]


def read_stamp(pkg_dir: Path) -> str | None:
    """The id this copy was built with, or None if it was never stamped."""
    stamp = Path(pkg_dir) / STAMP_NAME
    try:
        text = stamp.read_text().strip()
    except (OSError, ValueError):
        return None
    return text or None


def is_frozen(pkg_dir: Path) -> bool:
    """True when this copy carries a stamp, i.e. it was built, not edited."""
    return read_stamp(pkg_dir) is not None


def resolve(pkg_dir: Path) -> str:
    """The stamp if there is one, otherwise a hash of what is on disk.

    A stamped copy is never rehashed. Recomputing inside a zipapp would
    usually agree, but when it did not -- a truncated archive, a partial
    write -- it would report a plausible id rather than the one the archive
    shipped as, which is the opposite of what this module is for.
    """
    return read_stamp(pkg_dir) or compute(pkg_dir)


def build_id() -> str:
    """The build id of the running package, however it was installed.

    Reached through `importlib.resources` rather than `__file__` so it keeps
    working when the package is imported from inside the zipapp.
    """
    with as_file(files("webcompanion")) as pkg_dir:
        return resolve(Path(pkg_dir))


def skew_message(daemon_build: str | None, local_build: str | None = None) -> str | None:
    """One line naming the remedy, or None when the two sides agree.

    `None` for `daemon_build` is not "unknown, say nothing": a daemon that
    cannot name its build is one built before stamps existed, which is
    precisely the stale case worth reporting.
    """
    local = local_build if local_build is not None else build_id()
    if daemon_build == local:
        return None
    if not daemon_build:
        return ("webcompanion: the running daemon predates build stamps, so it is "
                "older than this CLI.\n"
                "  Rebuild and restart it:  webcompanion install-service")
    return (f"webcompanion: the running daemon is a different build than this CLI "
            f"(daemon {daemon_build}, cli {local}).\n"
            f"  Routes added since it started will answer 404.\n"
            f"  Rebuild and restart it:  webcompanion install-service")
