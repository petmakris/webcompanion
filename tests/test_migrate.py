from __future__ import annotations

import json

from webcompanion import items, paths
from webcompanion.commands import migrate
from webcompanion.config import Config
from webcompanion.registry import Registry


def _old_workspace(root, skill, sid, slug, blocks):
    ws = root / skill / "workspaces" / sid
    (ws / "response").mkdir(parents=True)
    (ws / "state").mkdir(parents=True)
    (ws / "response" / "blocks.json").write_text(json.dumps({"blocks": blocks}))
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
