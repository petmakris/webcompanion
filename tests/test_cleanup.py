from __future__ import annotations

import time
from pathlib import Path

from webcompanion import cleanup, paths
from webcompanion.atomic import write_text_atomic
from webcompanion.config import Config
from webcompanion.registry import Registry


def _session(reg, cfg, kind, sid):
    dirs = paths.make_session_dirs(cfg, kind, sid)
    reg.create(kind, sid, dirs, {"title": sid}, "/p")
    return dirs


def test_retention_defaults_to_infinite(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    dirs = _session(reg, cfg, "annotate", "old-one")
    ancient = time.time() - 86400 * 3650
    import os
    os.utime(paths.base_of(dirs), (ancient, ancient))
    assert cleanup.expire(cfg, reg) == 0
    assert paths.base_of(dirs).is_dir()


def test_retention_when_configured_removes_only_the_expired(tmp_path):
    import os
    cfg = Config(workspace_root=tmp_path / "ws", retention_days=30)
    reg = Registry(tmp_path / "state")
    old = _session(reg, cfg, "annotate", "old-one")
    new = _session(reg, cfg, "annotate", "new-one")
    ancient = time.time() - 86400 * 400
    os.utime(paths.base_of(old), (ancient, ancient))
    assert cleanup.expire(cfg, reg) == 1
    assert not paths.base_of(old).exists()
    assert paths.base_of(new).is_dir()


def test_the_stray_sweep_cannot_reach_another_kind(tmp_path):
    # One shared root means a registry bug in one client must not be able to
    # delete another client's workspaces.
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    kept = _session(reg, cfg, "deck", "deck-session")
    stray = paths.kind_root(cfg, "annotate") / "251231-000000-deadbeefdeadbeef"
    stray.mkdir(parents=True)
    assert cleanup.sweep_strays(cfg, "annotate", reg) == 1
    assert not stray.exists()
    assert paths.base_of(kept).is_dir()


def test_the_stray_sweep_cannot_reach_an_unregistered_sid_of_another_kind(tmp_path):
    """The brief's isolation test uses a non-sid-shaped `kept` sid
    ("deck-session"), and a registered one at that -- so it cannot fail
    against a mutation that widens the scan recursively but still filters by
    sid shape and by registration: such a mutation would never touch
    "deck-session" regardless of scope, sid-shaped or not, registered or not.

    This closes that gap with an UNREGISTERED, sid-shaped stray sitting in a
    different kind. Nothing but the kind-directory boundary protects it, so
    a scope-widening mutation that still respects sid-shape and registration
    has something real to delete here.
    """
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")  # empty on purpose: nothing is registered
    deck_stray = paths.kind_root(cfg, "deck") / "251231-000000-cafebabecafebabe"
    deck_stray.mkdir(parents=True)
    annotate_stray = paths.kind_root(cfg, "annotate") / "251231-000000-deadbeefdeadbeef"
    annotate_stray.mkdir(parents=True)
    assert cleanup.sweep_strays(cfg, "annotate", reg) == 1
    assert not annotate_stray.exists()
    assert deck_stray.exists()


def test_the_stray_sweep_ignores_directories_that_are_not_sid_shaped(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")  # empty on purpose: nothing is registered
    (paths.kind_root(cfg, "annotate") / "not-a-session").mkdir(parents=True)
    assert cleanup.sweep_strays(cfg, "annotate", reg) == 0


def test_a_registered_workspace_is_never_a_stray(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    dirs = _session(reg, cfg, "annotate", "251231-000000-abcdefabcdefabcd")
    reg.persist()
    cleanup.sweep(cfg, reg)
    assert paths.base_of(dirs).is_dir()


def test_rows_whose_directories_vanished_are_pruned(tmp_path):
    import shutil
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    dirs = _session(reg, cfg, "annotate", "doomed")
    sid = "doomed"
    shutil.rmtree(paths.base_of(dirs))
    assert cleanup.prune_dead_rows(reg) == 1
    assert reg.lookup(sid) is None


def test_a_marker_recreated_directory_is_healed_by_prune_then_sweep(tmp_path):
    """Task 10 hazard: server.py's `_mark` writes terminal markers via
    write_text_atomic, which does `path.parent.mkdir(parents=True,
    exist_ok=True)`. Superseding a sibling whose base directory was already
    deleted from disk silently recreates just its state_dir to drop the
    marker in it -- so afterwards a sid-shaped directory exists that the
    registry row still points at (not a stray by name-matching alone), but
    whose contents are a lone marker; the rest of the workspace (items_dir
    etc.) is gone.

    Deliberate choice: prune_dead_rows requires ALL of a row's directories to
    exist (matching Registry.rehydrate's own strictness), not just the base.
    A partially-recreated shell therefore counts as dead and gets
    unregistered. Once unregistered, the next sweep_strays call reclaims the
    now-orphaned shell as an ordinary stray. This is what makes `sweep()`
    self-heal the hazard instead of leaving a phantom directory with a
    dangling registry row forever.
    """
    sid = "251231-000000-1234567890abcdef"
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    dirs = _session(reg, cfg, "annotate", sid)
    base = paths.base_of(dirs)
    import shutil
    shutil.rmtree(base)

    # Simulate _mark() superseding this now-deleted sibling.
    write_text_atomic(Path(dirs["state_dir"]) / "finished", "")
    assert base.is_dir()
    assert not Path(dirs["items_dir"]).is_dir()

    assert cleanup.prune_dead_rows(reg) == 1
    assert reg.lookup(sid) is None
    assert cleanup.sweep_strays(cfg, "annotate", reg) == 1
    assert not base.exists()
