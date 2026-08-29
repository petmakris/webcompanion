"""Retention, the stray sweep, and dead-row pruning.

THIS IS A DELETION MODULE. Every function here removes a user's data with no
backup, so the bias throughout is: when a rule is ambiguous, do not delete.

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

`sweep` is the one function server.py calls: it runs `expire`, then
`prune_dead_rows`, then `sweep_strays` for every kind directory present under
the workspace root, in that order. That order matters for self-heal (below).

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
from webcompanion.registry import Registry

# Shape of server-minted sids (Registry.make_sid: "YYMMDD-HHMMSS-<16 hex>").
# The stray sweep only ever deletes directories matching this, so a user's
# own files that happen to live next to session dirs are never candidates,
# and directories that merely look session-ish by coincidence are left alone.
_SID_DIR_RE = re.compile(r"^\d{6}-\d{6}-[0-9a-f]{16}$")


def expire(cfg: Config, registry: Registry) -> int:
    """Delete workspaces whose base directory has been idle past
    `cfg.retention_days`. Returns 0 and touches nothing if retention is
    unconfigured -- the default -- because "infinite" must mean infinite,
    not "whatever a hardcoded fallback happens to be".
    """
    if cfg.retention_days is None:
        return 0
    cutoff = time.time() - cfg.retention_days * 86400
    removed = 0
    for sid, dirs in list(registry.items()):
        base = paths.base_of(dirs)
        try:
            mtime = base.stat().st_mtime
        except OSError:
            continue  # can't tell how old it is -- conservative: leave it
        if mtime > cutoff:
            continue
        try:
            shutil.rmtree(base)
        except OSError:
            continue  # deletion failed -- leave the row pointing at it
        registry.unregister(sid)
        removed += 1
    return removed


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
    for child in root.iterdir():
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


def sweep(cfg: Config, registry: Registry) -> dict[str, int]:
    """Run retention, dead-row pruning, and the stray sweep, in that order.

    Order matters: `expire` and `prune_dead_rows` can each turn a registered
    row into an unregistered one (by deleting it outright, or by dropping a
    row whose workspace is incomplete), and only then does `sweep_strays`
    walk each kind directory -- so anything freed up by the first two steps
    gets reclaimed in the same pass instead of lingering until the next
    restart.
    """
    expired = expire(cfg, registry)
    dead_rows_pruned = prune_dead_rows(registry)
    strays_removed = 0
    root = paths.workspace_root(cfg)
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or not paths.VALID_KIND_RE.match(child.name):
                continue
            strays_removed += sweep_strays(cfg, child.name, registry)
    return {
        "expired": expired,
        "dead_rows_pruned": dead_rows_pruned,
        "strays_removed": strays_removed,
    }
