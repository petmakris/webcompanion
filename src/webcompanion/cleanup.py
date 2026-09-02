"""Retention, the stray sweep, dead-row pruning, and idle auto-expiry.

THIS IS MOSTLY A DELETION MODULE. Every function here removes a user's data
with no backup except `expire_idle`, the one exception -- it never deletes
anything, it only ever marks a still-live session `finished` (the same
marker-file write `webcompanion end` already performs). It lives in this
module anyway because it shares `_last_activity`'s idleness definition with
`expire`, not because it deletes anything; see its own docstring and
docs/2026-09-02-session-lifecycle-design.md Decision 1 and Decision 4. The
bias for every deleting function below is still: when a rule is ambiguous,
do not delete.

- `expire` only ever runs if `cfg.retention_days` is set. It defaults to
  `None` -- infinite -- because `resume <slug>` is a shipped feature and
  workspaces go back to install day; the daemon must not start deleting them
  just because configuration moved from the environment to a config file.
- `sweep_strays` runs per kind, inside `<workspace_root>/<kind>/` only. One
  shared root now holds what used to live under five separate ones, so
  scoping the sweep to one kind is what makes it impossible for a registry
  bug affecting one kind to delete another kind's workspaces.
- `prune_dead_rows` drops registry rows whose directories are gone or
  incomplete -- see the note on the Task 10 hazard below.
- Every directory scan (`root.iterdir()`) is wrapped in `try/except OSError`
  and skips just that root on failure. A cleanup pass must never be able to
  stop the daemon from booting: `Daemon.start()` also wraps the whole
  `cleanup.sweep(...)` call, but the scans guard themselves too, so one
  unreadable kind directory does not abort the pass over the others even
  when `sweep_strays` or `sweep` is called directly.

`sweep` is the one function server.py calls: it runs `expire`, then
`prune_dead_rows`, then `sweep_strays` for every kind directory present under
the workspace root, in that order. That order matters for self-heal (below).

When the registry cannot be trusted, the stray pass is REFUSED
--------------------------------------------------------------
`sweep_strays` deletes what no registry row points at, so it is only ever as
safe as the registry is complete. Two states make it incomplete, and in both
of them an empty registry means "we do not know what is live", not "nothing
is live":

- `sessions.json` exists but did not parse (`Registry.rehydrate` returns
  UNREADABLE). The daemon has no idea what is registered.
- The registry is empty while the workspace root is not. Every sid-shaped
  directory under it would be a stray, i.e. the sweep would delete
  everything -- which is never a legitimate outcome of an ordinary boot.
- The registry SHRANK without the workspaces shrinking with it: the pass
  would delete at least two directories AND more than half of every
  sid-shaped directory present. See `_SHRINK_MIN_DOOMED` for the rule and
  why it is a ratio rather than the boolean it replaces -- a boolean
  emptiness test is disarmed by a single newly created session, which is
  how 3 workspaces were destroyed two boots after one corrupt registry.

`sweep` refuses the stray pass in both cases and reports why in
`strays_refused`, so `Daemon.start()` can record it in the durable marker
`doctor` reads. `expire` and `prune_dead_rows` still run: neither deletes on
the strength of a row's ABSENCE.

Task 10 hazard
--------------
`server.py`'s `_mark` writes terminal markers via `write_text_atomic`, which
does `path.parent.mkdir(parents=True, exist_ok=True)`. So superseding a
sibling whose base directory was already deleted from disk silently
RECREATES just that sibling's `state_dir` (and nothing else) to drop the
marker in it. The result is a sid-shaped directory a registry row DOES point
at -- so `sweep_strays` alone would never call it a stray -- but whose
contents are a lone marker file; the rest of the workspace (`items_dir`,
`threads_dir`, ...) never comes back.

Deliberate choice: `prune_dead_rows` requires ALL of a row's directories to
exist, the same strictness `Registry.rehydrate` already applies, not merely
that the base directory exists. A partially-recreated shell fails that check
and gets unregistered. Once unregistered, the next `sweep_strays` pass (which
`sweep` always runs afterwards) reclaims the now-orphaned shell as an
ordinary stray. That is what makes `sweep()` self-heal the hazard, rather
than leaving a phantom directory with a dangling registry row forever.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from webcompanion import paths
from webcompanion.config import Config
from webcompanion.registry import UNREADABLE, Registry

# Shape of server-minted sids (Registry.make_sid: "YYMMDD-HHMMSS-<16 hex>").
# The stray sweep only ever deletes directories matching this, so a user's
# own files that happen to live next to session dirs are never candidates,
# and directories that merely look session-ish by coincidence are left alone.
_SID_DIR_RE = re.compile(r"^\d{6}-\d{6}-[0-9a-f]{16}$")


# A workspace's own base directory's mtime only advances when a direct
# child of BASE is created or removed -- not when something is written
# inside items_dir, threads_dir, or state_dir. Annotation writes
# (items.put/put_many/delete) go straight into items_dir and never touch
# base, so base's mtime stops advancing right after creation. Taking base
# alone as "last activity" would let a session under active annotation
# expire mid-use the moment retention is configured. Take the newest mtime
# across base and its meaningful children instead.
_AGE_CHILDREN = ("items_dir", "threads_dir", "state_dir")


def _last_activity(dirs: dict) -> float | None:
    candidates: list[float] = []
    try:
        candidates.append(paths.base_of(dirs).stat().st_mtime)
    except OSError:
        pass
    for key in _AGE_CHILDREN:
        value = dirs.get(key)
        if value is None:
            continue
        try:
            candidates.append(Path(value).stat().st_mtime)
        except OSError:
            continue
    return max(candidates) if candidates else None


def expire(cfg: Config, registry: Registry) -> int:
    """Delete workspaces idle past `cfg.retention_days`, measuring idleness
    by the newest mtime across the workspace's base dir and its meaningful
    children (see `_last_activity`). Returns 0 and touches nothing if
    retention is unconfigured -- the default -- because "infinite" must mean
    infinite, not "whatever a hardcoded fallback happens to be".
    """
    if cfg.retention_days is None:
        return 0
    cutoff = time.time() - cfg.retention_days * 86400
    removed = 0
    for sid, dirs in list(registry.items()):
        activity = _last_activity(dirs)
        if activity is None:
            continue  # can't tell how old it is -- conservative: leave it
        if activity > cutoff:
            continue
        base = paths.base_of(dirs)
        try:
            shutil.rmtree(base)
        except OSError:
            continue  # deletion failed -- leave the row pointing at it
        registry.unregister(sid)
        removed += 1
    return removed


def expire_idle(cfg: Config, registry: Registry, *,
                 is_terminal, mark_finished) -> int:
    """Mark `finished` any live session idle past `cfg.idle_expiry_hours`.

    Unlike `expire()`, this never deletes anything -- it only ever calls
    `mark_finished` (the same marker-file write `webcompanion end` already
    performs), and only ever touches a session `is_terminal` reports as not
    already finished/cancelled. Safety net for skills that never call `end`
    themselves; see docs/2026-09-02-session-lifecycle-design.md.

    `is_terminal`/`mark_finished` are injected (not imported from `server.py`)
    to keep this module's existing independence from `server.py` -- avoids
    introducing a `server -> cleanup -> server` import cycle, since `server.py`
    already imports `cleanup` for the existing one-shot `sweep()` call.
    `is_terminal(state_dir: Path) -> bool`; `mark_finished(state_dir: Path)
    -> None`.

    Idleness is measured with `_last_activity`, the exact helper `expire`
    uses for `retention_days` -- one definition of "how stale is this
    workspace" for the whole module. A session `_last_activity` cannot read
    (its dirs are unreadable or missing) is left alone, the same
    conservative "can't tell -- leave it" `expire` already applies.

    Returns 0 and touches nothing if auto-expiry is unconfigured
    (`cfg.idle_expiry_hours is None`) -- unlike `retention_days`, that is not
    this feature's default state, but it must still be possible to switch
    off from the config file (see `config._int_or_none`).
    """
    if cfg.idle_expiry_hours is None:
        return 0
    cutoff = time.time() - cfg.idle_expiry_hours * 3600
    marked = 0
    for sid, dirs in list(registry.items()):
        state_dir = Path(dirs["state_dir"])
        if is_terminal(state_dir):
            continue  # already finished/cancelled -- nothing to do
        activity = _last_activity(dirs)
        if activity is None:
            continue  # can't tell how idle it is -- conservative: leave it
        if activity > cutoff:
            continue
        mark_finished(state_dir)
        registry.note_change(sid)
        marked += 1
    return marked


def sweep_strays(cfg: Config, kind: str, registry: Registry) -> int:
    """Remove sid-shaped directories under `<workspace_root>/<kind>/` that no
    registry row points at. Never looks outside that one kind directory.

    `registry` is required, not optional, on purpose: every function in this
    module deletes a user's data, and a default here would mean "delete
    without checking what is live" is what happens when a caller forgets one
    argument. There is no safe default in a deletion module -- the safe
    choice is to force the caller to say explicitly what is registered, even
    if that means passing an empty `Registry()` to mean "nothing is". Do not
    restore a `= None` default: Task 17's `migrate` command is a second
    caller of this function, and it must not be able to wipe a kind's
    workspaces by omission.
    """
    root = paths.kind_root(cfg, kind)  # raises ValueError on a bad kind
    if not root.is_dir():
        return 0
    registered = {sid for sid, _ in registry.items()}
    removed = 0
    try:
        children = list(root.iterdir())
    except OSError:
        return 0  # can't read this kind's directory -- skip it, don't crash
    for child in children:
        if not child.is_dir():
            continue
        if not _SID_DIR_RE.match(child.name):
            continue
        if child.name in registered:
            continue
        try:
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed


def prune_dead_rows(registry: Registry) -> int:
    """Unregister rows whose workspace is gone or incomplete.

    "Incomplete" (not merely "base dir missing") matters for the Task 10
    hazard documented above: `_mark` can recreate a lone `state_dir` for a
    sibling whose real workspace was already deleted, and that shell must
    count as dead so it can be reclaimed. This mirrors the strictness
    `Registry.rehydrate` already applies on daemon start.
    """
    removed = 0
    for sid, dirs in list(registry.items()):
        real = [v for k, v in dirs.items() if not str(k).startswith("_")]
        if real and all(Path(p).is_dir() for p in real):
            continue
        registry.unregister(sid)
        removed += 1
    return removed


# The shrink detector. `sweep_strays` deletes every sid-shaped directory no
# registry row points at, so the number it would delete IS the blast radius,
# and the registry shrinking without the directories shrinking with it is the
# signature of a lost registry rather than of ordinary garbage.
#
# THE RULE: refuse the stray pass when the sweep would delete at least
# `_SHRINK_MIN_DOOMED` directories AND more than half of all the sid-shaped
# directories present.
#
# Why both halves, and why these numbers:
#
# - "More than half" is what makes it a SHRINK detector rather than an
#   emptiness test. The guard this replaces asked `not registry.items()`,
#   a boolean that one newly created session disarms: after a lost registry,
#   1 row against 4 directories passed it and the sweep removed the other 3.
#   A ratio cannot be disarmed that way -- 1 of 4 is still a shrink.
# - "At least two" is what keeps ordinary operation working. Reaping a single
#   stray is the common, legitimate case (a crashed create, a hand-deleted
#   row) and it is never evidence of a lost registry, even when it is the only
#   directory under a kind. Without this floor, 0-of-1 and 1-of-2 would refuse
#   forever and strays would accumulate with nothing able to reclaim them.
#
# Worked through: 1 row / 4 dirs -> 3 doomed, 3 >= 2 and 3 > 2 -> REFUSED (the
# reproduced data loss). 10 rows / 12 dirs -> 2 doomed, 2 > 6 is false ->
# allowed. 1 row / 2 dirs -> 1 doomed, below the floor -> allowed.
#
# An empty registry facing ANY sid-shaped directory is still refused outright,
# below, independently of this rule: there the sweep would delete everything,
# which is never a legitimate outcome of an ordinary boot.
_SHRINK_MIN_DOOMED = 2


def _sid_dirs(cfg: Config) -> tuple[list[str], bool]:
    """(names of every sid-shaped directory under the workspace root,
    whether the scan was complete).

    Answers both "is there anything here the sweep could delete?" and "how
    much of it would go?". An unreadable directory makes the scan
    incomplete; the caller treats "cannot tell" as "there is something", the
    conservative reading in a deletion module.
    """
    names: list[str] = []
    root = paths.workspace_root(cfg)
    if not root.is_dir():
        return names, True
    try:
        kinds = list(root.iterdir())
    except OSError:
        return names, False
    complete = True
    for kind_dir in kinds:
        if not kind_dir.is_dir() or not paths.VALID_KIND_RE.match(kind_dir.name):
            continue
        try:
            children = list(kind_dir.iterdir())
        except OSError:
            complete = False
            continue
        for child in children:
            if child.is_dir() and _SID_DIR_RE.match(child.name):
                names.append(child.name)
    return names, complete


def sweep(cfg: Config, registry: Registry, *,
          registry_status: str = "loaded") -> dict:
    """Run retention, dead-row pruning, and the stray sweep, in that order.

    Order matters: `expire` and `prune_dead_rows` can each turn a registered
    row into an unregistered one (by deleting it outright, or by dropping a
    row whose workspace is incomplete), and only then does `sweep_strays`
    walk each kind directory -- so anything freed up by the first two steps
    gets reclaimed in the same pass instead of lingering until the next
    restart.

    `registry_status` is what `Registry.rehydrate()` returned. The stray
    pass is refused unless the registry can be trusted -- see "When the
    registry cannot be trusted" in the module docstring. `strays_refused`
    in the returned summary is None when the pass ran, and a human-readable
    reason when it did not.
    """
    expired = expire(cfg, registry)
    dead_rows_pruned = prune_dead_rows(registry)

    refused: str | None = None
    if registry_status == UNREADABLE:
        refused = (f"{registry.sessions_file} exists but could not be parsed; "
                   f"the daemon cannot tell which workspaces are live")
    else:
        registered = {sid for sid, _ in registry.items()}
        sid_dirs, scan_complete = _sid_dirs(cfg)
        doomed = [name for name in sid_dirs if name not in registered]
        if not registered and (sid_dirs or not scan_complete):
            refused = (f"the registry is empty but {paths.workspace_root(cfg)} "
                       f"still holds session directories; every one of them would "
                       f"have been deleted as a stray")
        elif len(doomed) >= _SHRINK_MIN_DOOMED and len(doomed) * 2 > len(sid_dirs):
            refused = (f"the registry shrank without the workspaces shrinking "
                       f"with it: {len(registered)} live registry row(s) against "
                       f"{len(sid_dirs)} session directories under "
                       f"{paths.workspace_root(cfg)}, so the stray pass would "
                       f"have deleted {len(doomed)} of them")

    strays_removed = 0
    if refused is None:
        root = paths.workspace_root(cfg)
        if root.is_dir():
            try:
                children = list(root.iterdir())
            except OSError:
                children = []  # can't read the workspace root -- skip strays
            for child in children:
                if not child.is_dir() or not paths.VALID_KIND_RE.match(child.name):
                    continue
                strays_removed += sweep_strays(cfg, child.name, registry)
    return {
        "expired": expired,
        "dead_rows_pruned": dead_rows_pruned,
        "strays_removed": strays_removed,
        "strays_refused": refused,
    }
