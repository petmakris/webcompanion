from __future__ import annotations

import json
from pathlib import Path

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
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    result = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)
    assert result["moved"] == 1
    assert (paths.kind_root(cfg, "annotate") / "s1").is_dir()


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
    old = tmp_path / "old"
    _old_workspace(old, "annotate", "s1", "my-plan", [{"id": "b-1", "markdown": "hi"}])
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    p = migrate.plan([old / "annotate"])
    migrate.apply(p, cfg, reg)
    second = migrate.apply(migrate.plan([old / "annotate"]), cfg, reg)
    assert second["moved"] == 0


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
