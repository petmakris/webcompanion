from __future__ import annotations

import json
from pathlib import Path

import pytest

from webcompanion import items, paths, threads
from webcompanion.commands import migrate
from webcompanion.config import Config
from webcompanion.registry import Registry


def _old_workspace(root, skill, sid, slug, blocks, thread_payloads=None):
    ws = root / skill / "workspaces" / sid
    (ws / "response").mkdir(parents=True)
    (ws / "state").mkdir(parents=True)
    (ws / "response" / "blocks.json").write_text(json.dumps({"blocks": blocks}))
    if thread_payloads:
        # Old layout: <base>/state/threads/<anchor>.json -- a different
        # directory from the new threads_dir (<base>/threads/).
        old_threads_dir = ws / "state" / "threads"
        old_threads_dir.mkdir(parents=True)
        for i, payload in enumerate(thread_payloads):
            (old_threads_dir / f"thread-{i}.json").write_text(json.dumps(payload))
    (root / skill).mkdir(parents=True, exist_ok=True)
    (root / skill / "sessions.json").write_text(json.dumps({
        sid: {"response_dir": str(ws / "response"), "state_dir": str(ws / "state"),
              "_cwd": "/proj", "_sid": sid}}))
    (root / skill / "sessions_meta.json").write_text(json.dumps({
        sid: {"slug": slug, "title": slug}}))
    return ws


def test_a_workspace_moves_under_its_kind(tmp_path):
    # Checking only that the destination directory exists is not enough:
    # make_session_dirs() creates that whole tree unconditionally, so this
    # would still pass even if the shutil.move() were dropped entirely.
    # Assert the actual content arrived, not just the skeleton.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)
    assert result["moved"] == 1
    assert (paths.kind_root(cfg, "annotate") / "s1").is_dir()
    dirs = reg.lookup("s1")
    stored = items.load_all(dirs["items_dir"])
    assert stored["b-1"]["markdown"] == "hi"


def test_blocks_become_items(tmp_path):
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan",
                   [{"id": "b-1", "markdown": "hello"},
                    {"id": "b-2", "markdown": "world"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)
    dirs = reg.lookup("s1")
    stored = items.load_all(dirs["items_dir"])
    assert set(stored) == {"b-1", "b-2"}
    assert stored["b-1"]["markdown"] == "hello"


def test_the_slug_and_the_kind_survive(tmp_path):
    old = tmp_path / "old"
    _old_workspace(old, "deck", "s1", "my-deck", [])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    migrate.apply(migrate.plan([old / "deck"]), cfg, reg)
    assert reg.resolve("my-deck", kind="deck") == "s1"
    assert reg.get_meta("s1")["kind"] == "deck"


def test_a_slug_shared_by_two_kinds_is_no_longer_a_collision(tmp_path):
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "plan", [])
    _old_workspace(old, "deck", "s2", "plan", [])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    migrate.apply(migrate.plan([old / "annotate", old / "deck"]), cfg, reg)
    assert reg.resolve("plan", kind="annotate") == "s1"
    assert reg.resolve("plan", kind="deck") == "s2"


def test_a_workspace_whose_content_cannot_be_read_is_marked_read_only(tmp_path):
    old = tmp_path / "old"
    ws = _old_workspace(old, "annotate", "s1", "broken", [])
    (ws / "response" / "blocks.json").write_text("{not json")
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)
    assert result["read_only"] == 1
    assert reg.get_meta("s1").get("read_only") is True


def test_migration_is_idempotent(tmp_path):
    # Reusing one in-memory Registry object across both calls would prove
    # idempotency only via registry.lookup() in memory -- it would never
    # exercise the disk-based path (old_base/new_base existence) a real
    # crash-and-restart depends on. Use a FRESH, rehydrated Registry for the
    # second call, the way a restarted CLI process actually would.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    state_root = tmp_path / "state"
    reg = Registry(state_root)
    migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)

    fresh = Registry(state_root)
    fresh.rehydrate()
    second = migrate.apply(migrate.plan([old / "annotate"]), cfg, fresh)
    assert second["moved"] == 0
    assert second["recovered"] == 0
    assert second["already_done"] == 1


def test_plan_reports_what_it_will_do_without_touching_anything(tmp_path):
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [])
    cfg = Config(workspace_root=tmp_path / "ws")
    p = migrate.plan([old / "annotate"])
    assert p and p[0]["sid"] == "s1" and p[0]["kind"] == "annotate"
    assert not paths.kind_root(cfg, "annotate").exists()


def test_plan_creates_no_new_directory_anywhere(tmp_path):
    # Stronger than the check above: rather than asking about one path a
    # bug might happen to miss, this asks about every directory under
    # tmp_path, so a plan() that mkdirs ANY new destination -- under any
    # config, including one plan() never even sees -- gets caught. This is
    # the shape of check that would have caught it if `plan()` had ever
    # created its `new_base` eagerly instead of just naming it.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    before = {p for p in tmp_path.rglob("*") if p.is_dir()}
    migrate.plan([old / "annotate"])
    after = {p for p in tmp_path.rglob("*") if p.is_dir()}
    assert after == before


def test_non_annotate_content_is_needs_repush_not_read_only(tmp_path):
    # deck/dataflow/walkthrough/interactive_review never wrote blocks.json in
    # the old layout -- their content lives in their own state files. That is
    # not corruption: it must not be reported the same way a genuinely
    # unreadable annotate session is.
    old = tmp_path / "old"
    _old_workspace(old, "walkthrough", "s1", "my-walk", [])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    result = migrate.apply(migrate.plan([old / "walkthrough"]), cfg, reg)
    assert result["needs_repush"] == 1
    assert result["read_only"] == 0
    meta = reg.get_meta("s1")
    assert meta.get("needs_repush") is True
    assert not meta.get("read_only")


def test_threads_move_into_the_new_threads_dir(tmp_path):
    old = tmp_path / "old"
    thread_payloads = [
        {"anchor": "a.py:R:10", "version": 1, "messages": [{"role": "user", "text": "hi"}]},
        {"anchor": "a.py:R:20", "version": 2, "messages": [{"role": "user", "text": "there"}]},
    ]
    ws = _old_workspace(old, "interactive_review", "s1", "my-review", [],
                         thread_payloads=thread_payloads)
    before_count = len(list((ws / "state" / "threads").glob("*.json")))
    assert before_count == 2

    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    migrate.apply(migrate.plan([old / "interactive_review"]), cfg, reg)

    dirs = reg.lookup("s1")
    after_count = len(list((paths.base_of(dirs) / "state" / "threads").glob("*.json"))) \
        if (paths.base_of(dirs) / "state" / "threads").is_dir() else 0
    new_threads_count = len(list(Path(dirs["threads_dir"]).glob("*.json")))

    assert after_count == 0, "nothing should be left behind in the old threads dir"
    assert new_threads_count == before_count == 2
    assert threads.list_versions(dirs["threads_dir"]) == {
        "a.py:R:10": 1,
        "a.py:R:20": 2,
    }


def test_plan_reports_the_resolved_absolute_source_path(tmp_path):
    # The exact line that would have prevented the real-data incident: a
    # copied registry still names the ORIGINAL absolute directories inside
    # its sessions.json, so old_base here is not "wherever old_roots points"
    # -- it is the literal absolute path apply() will touch.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [])
    p = migrate.plan([old / "annotate"])
    assert p[0]["old_base"].is_absolute()
    assert p[0]["old_base"] == old / "annotate" / "workspaces" / "s1"


def test_into_copies_and_leaves_every_source_untouched(tmp_path):
    old = tmp_path / "old"
    thread_payloads = [{"anchor": "a.py:R:1", "version": 1, "messages": []}]
    ws = _old_workspace(old, "annotate", "s1", "my-plan",
                        [{"id": "b-1", "markdown": "hi"}],
                        thread_payloads=thread_payloads)
    cfg = Config(workspace_root=tmp_path / "ws")  # ignored: --into overrides the destination
    reg = Registry(tmp_path / "state")
    into_dir = tmp_path / "rehearsal"

    before_files = {p.relative_to(ws): p.read_bytes() for p in ws.rglob("*") if p.is_file()}

    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg, into=into_dir)

    # Every source file present, byte-identical, nothing removed.
    after_files = {p.relative_to(ws): p.read_bytes() for p in ws.rglob("*") if p.is_file()}
    assert after_files == before_files
    assert ws.is_dir()
    assert (old / "annotate" / "sessions.json").exists()

    # A complete result landed in the target: moved/migrated counts, items
    # converted, and dirs point under into_dir, not under cfg's workspace_root.
    assert result["moved"] == 1
    assert result["migrated"] == 1
    dirs = reg.lookup("s1")
    assert Path(dirs["items_dir"]).is_relative_to(into_dir)
    assert not Path(dirs["items_dir"]).is_relative_to(cfg.workspace_root)
    stored = items.load_all(dirs["items_dir"])
    assert stored["b-1"]["markdown"] == "hi"
    assert threads.list_versions(dirs["threads_dir"]) == {"a.py:R:1": 1}


def test_a_crash_between_move_and_registration_is_recovered_on_retry(tmp_path, monkeypatch):
    # Reproduces the Critical: kill the process between shutil.move landing
    # the data at the destination and registry.create() writing the row
    # that makes it findable. A naive retry that keys "already done" off
    # old_base being gone would classify this orphan as finished forever.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    state_root = tmp_path / "state"
    reg = Registry(state_root)

    real_create = Registry.create

    def boom(self, *a, **kw):
        raise RuntimeError("simulated crash after the move, before registration")

    monkeypatch.setattr(Registry, "create", boom)

    rows = migrate.plan([old / "annotate"])
    with pytest.raises(RuntimeError):
        migrate.apply(rows, cfg, reg)

    # The crash reproduced the reported symptom exactly: data moved, source
    # gone, nothing registered.
    dest = paths.kind_root(cfg, "annotate") / "s1"
    assert dest.is_dir()
    assert not (old / "annotate" / "workspaces" / "s1").exists()

    monkeypatch.setattr(Registry, "create", real_create)

    # A fresh, rehydrated Registry -- the way a restarted CLI process would
    # see the world -- knows nothing about s1 either.
    fresh = Registry(state_root)
    fresh.rehydrate()
    assert fresh.lookup("s1") is None

    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, fresh)

    assert result["recovered"] == 1
    assert result["moved"] == 0        # no transfer happened this run -- it was already there
    assert result["already_done"] == 0
    assert fresh.resolve("my-plan", kind="annotate") == "s1"
    stored = items.load_all(fresh.lookup("s1")["items_dir"])
    assert stored["b-1"]["markdown"] == "hi"


def test_both_source_and_destination_present_is_left_for_a_human(tmp_path):
    # Neither side is registered, but data sits on both sides -- there is no
    # safe way to guess which copy is authoritative. Must not silently move
    # or silently drop either one.
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")

    dest = paths.kind_root(cfg, "annotate") / "s1"
    dest.mkdir(parents=True)
    (dest / "sentinel.txt").write_text("something already here, unrelated")

    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)

    assert result["needs_attention"] == 1
    assert result["moved"] == 0
    assert result["recovered"] == 0
    assert reg.lookup("s1") is None
    # Neither side was touched.
    assert (old / "annotate" / "workspaces" / "s1").is_dir()
    assert (dest / "sentinel.txt").exists()


# ── containment: a row must live under the root it was read from ─────────

def test_a_row_pointing_outside_its_own_root_is_flagged_not_moved(tmp_path):
    """The real fix for the incident that produced `--into`.

    Copying an old root's tree to a scratch location does not rewrite the
    absolute paths inside its `sessions.json`, so `plan()` pointed at the
    copy still resolves `old_base` to the ORIGINAL, live directory -- and
    `apply()` moved 30 real workspaces. A legitimate legacy row always lives
    under the root it was read from; one that does not is refused.
    """
    elsewhere = tmp_path / "the-real-live-one" / "sessions" / "s1"
    (elsewhere / "state").mkdir(parents=True)

    copied_root = tmp_path / "scratch" / "annotate"
    copied_root.mkdir(parents=True)
    (copied_root / "sessions.json").write_text(json.dumps({
        "s1": {"state_dir": str(elsewhere / "state"), "_cwd": str(tmp_path)}}))

    rows = migrate.plan([copied_root])
    assert len(rows) == 1
    assert rows[0]["contained"] is False

    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    summary = migrate.apply(rows, cfg, reg)

    assert summary["needs_attention"] == 1
    assert summary["moved"] == 0
    assert elsewhere.is_dir(), "the live directory was moved anyway"


def test_an_ordinary_row_under_its_own_root_is_contained_and_moves(tmp_path):
    """The negative control: containment must not refuse the normal case."""
    old_root = tmp_path / "old" / "annotate"
    base = old_root / "sessions" / "s1"
    (base / "state").mkdir(parents=True)
    (old_root / "sessions.json").write_text(json.dumps({
        "s1": {"state_dir": str(base / "state"), "_cwd": str(tmp_path)}}))

    rows = migrate.plan([old_root])
    assert rows[0]["contained"] is True

    cfg = Config(workspace_root=tmp_path / "ws")
    summary = migrate.apply(rows, cfg, Registry(tmp_path / "state"))
    assert summary["moved"] == 1
    assert summary["needs_attention"] == 0
