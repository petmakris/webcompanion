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
    # Age EVERY meaningful directory, not just base: activity is measured as
    # the newest mtime across base, items_dir, threads_dir and state_dir, so
    # a genuinely dormant workspace has all of them old, not just its base.
    os.utime(paths.base_of(old), (ancient, ancient))
    for key in ("items_dir", "threads_dir", "state_dir"):
        os.utime(old[key], (ancient, ancient))
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
    # Asserting only the count is not enough -- a mutation that deletes the
    # directory but still returns 0 would pass. Assert what survives too.
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")  # empty on purpose: nothing is registered
    not_a_session = paths.kind_root(cfg, "annotate") / "not-a-session"
    not_a_session.mkdir(parents=True)
    assert cleanup.sweep_strays(cfg, "annotate", reg) == 0
    assert not_a_session.is_dir()


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


def test_expire_does_not_delete_a_workspace_under_active_annotation(tmp_path):
    """base_of(dirs)'s own mtime only advances when a direct child of BASE
    changes -- annotation writes (items.put) go straight into items_dir and
    never touch base, so base's mtime stops advancing right after creation.
    Taking base alone as "last activity" would let a session under active
    annotation expire mid-use the moment retention is configured.

    This ages base past the cutoff, then writes an item (touching items_dir,
    not base), and asserts the workspace SURVIVES the sweep despite base
    itself looking ancient. Fails against an implementation that only checks
    base's mtime.
    """
    import os

    from webcompanion import items

    cfg = Config(workspace_root=tmp_path / "ws", retention_days=30)
    reg = Registry(tmp_path / "state")
    dirs = _session(reg, cfg, "annotate", "active-one")
    ancient = time.time() - 86400 * 400
    os.utime(paths.base_of(dirs), (ancient, ancient))

    items.put(dirs["items_dir"], "line:1", {"text": "still being annotated"})

    assert cleanup.expire(cfg, reg) == 0
    assert paths.base_of(dirs).is_dir()


def test_an_unreadable_kind_directory_does_not_abort_the_rest_of_the_sweep(tmp_path):
    """A permission error inside root.iterdir() must not raise out of
    sweep() -- Daemon.start() calls cleanup.sweep() before binding its
    socket, so an unhandled exception here means the daemon never starts.
    The scan itself must be guarded, not just the per-child shutil.rmtree.

    Makes "annotate"'s kind directory unreadable, then asserts sweep()
    still completes and still reclaims a stray sitting in "deck".

    A live session is registered first so the registry is non-empty: an
    EMPTY registry facing a populated workspace root is the C2 shape, and
    sweep() now refuses the stray pass there rather than deleting
    everything. That refusal is a different behaviour from the one under
    test here.
    """
    import os

    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")

    live_sid = "251231-000000-0123456789abcdef"
    live_dirs = paths.make_session_dirs(cfg, "deck", live_sid)
    reg.create("deck", live_sid, live_dirs, {"title": "live"}, str(tmp_path))

    deck_stray = paths.kind_root(cfg, "deck") / "251231-000000-cafebabecafebabe"
    deck_stray.mkdir(parents=True)

    annotate_root = paths.kind_root(cfg, "annotate")
    annotate_root.mkdir(parents=True)
    original_mode = annotate_root.stat().st_mode
    os.chmod(annotate_root, 0)
    try:
        summary = cleanup.sweep(cfg, reg)  # must not raise
    finally:
        os.chmod(annotate_root, original_mode)  # tmp_path cleanup needs this back

    assert isinstance(summary, dict)
    assert summary["strays_refused"] is None
    assert not deck_stray.exists()  # the readable kind was still swept
    assert paths.base_of(live_dirs).is_dir()  # the registered one was kept


# ── the stray pass is refused when the registry cannot be trusted ────────

def test_the_stray_pass_is_refused_when_the_registry_did_not_parse(tmp_path):
    """`sweep_strays` deletes what no row points at, so it is only as safe
    as the registry is complete. An unreadable sessions.json means the
    daemon has no idea what is live -- not that nothing is."""
    from webcompanion import registry as regmod

    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    orphan = paths.kind_root(cfg, "annotate") / "251231-000000-cafebabecafebabe"
    orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg, registry_status=regmod.UNREADABLE)

    assert summary["strays_removed"] == 0
    assert "could not be parsed" in summary["strays_refused"]
    assert orphan.is_dir()


def test_the_stray_pass_is_refused_when_an_empty_registry_faces_a_full_root(tmp_path):
    """The second untrusted shape: every workspace would be a stray, which
    is never a legitimate outcome of an ordinary boot."""
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    orphan = paths.kind_root(cfg, "deck") / "251231-000000-cafebabecafebabe"
    orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg)

    assert summary["strays_removed"] == 0
    assert "registry is empty" in summary["strays_refused"]
    assert orphan.is_dir()


def test_a_genuine_stray_beside_a_live_session_is_still_reclaimed(tmp_path):
    """The negative control. Refusing on an EMPTY registry must not turn
    into refusing always -- one live row is enough to trust the sweep, and
    ordinary garbage collection has to keep working."""
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    live = "251231-000000-0123456789abcdef"
    reg.create("deck", live, paths.make_session_dirs(cfg, "deck", live),
               {"title": "live"}, str(tmp_path))
    orphan = paths.kind_root(cfg, "deck") / "251231-000000-cafebabecafebabe"
    orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg)

    assert summary["strays_refused"] is None
    assert summary["strays_removed"] == 1
    assert not orphan.exists()


def test_the_stray_pass_is_refused_when_the_registry_shrank_under_the_workspaces(
        tmp_path):
    """The guard this replaces was `not registry.items()`, a boolean, so one
    newly created session disarmed it: after a lost registry, 1 row against
    4 directories passed and the sweep deleted the other 3.

    The exact shape that destroyed data: 1 live row, 4 sid-shaped
    directories. The refusal must NAME the counts -- the message is what a
    user has to act on.
    """
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    live = "251231-000000-0123456789abcdef"
    reg.create("annotate", live, paths.make_session_dirs(cfg, "annotate", live),
               {"title": "live"}, str(tmp_path))
    orphans = [paths.kind_root(cfg, "annotate") / f"251231-000000-cafebabecafeba0{i}"
               for i in range(3)]
    for orphan in orphans:
        orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg)

    assert summary["strays_removed"] == 0
    assert all(orphan.is_dir() for orphan in orphans)
    assert "1 live registry row" in summary["strays_refused"]
    assert "4 session directories" in summary["strays_refused"]
    assert "deleted 3 of them" in summary["strays_refused"]


def test_ordinary_garbage_collection_still_reaps_a_stray_or_two(tmp_path):
    """The negative control for the shrink detector. Refusing a drastic
    shrink must not turn into refusing routine cleanup: 10 live rows against
    12 directories is 2 strays, and they have to go."""
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    for i in range(10):
        sid = f"251231-000000-000000000000000{i}"
        reg.create("deck", sid, paths.make_session_dirs(cfg, "deck", sid),
                   {"title": f"live {i}"}, str(tmp_path))
    orphans = [paths.kind_root(cfg, "deck") / f"251231-000000-cafebabecafeba0{i}"
               for i in range(2)]
    for orphan in orphans:
        orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg)

    assert summary["strays_refused"] is None
    assert summary["strays_removed"] == 2
    assert not any(orphan.exists() for orphan in orphans)


def test_a_lone_stray_under_a_kind_is_still_reaped(tmp_path):
    """The floor in the shrink rule, asserted. One live row and one stray is
    100% of the directories the sweep would delete being strays, which the
    ratio alone would refuse -- and then a single stray could never be
    reclaimed by anything."""
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    live = "251231-000000-0123456789abcdef"
    reg.create("deck", live, paths.make_session_dirs(cfg, "deck", live),
               {"title": "live"}, str(tmp_path))
    orphan = paths.kind_root(cfg, "annotate") / "251231-000000-cafebabecafebabe"
    orphan.mkdir(parents=True)

    summary = cleanup.sweep(cfg, reg)

    assert summary["strays_refused"] is None
    assert summary["strays_removed"] == 1
    assert not orphan.exists()
