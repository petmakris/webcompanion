from __future__ import annotations

from pathlib import Path

import pytest

from webcompanion import paths
from webcompanion.config import Config


def test_workspaces_are_namespaced_by_kind(tmp_path):
    cfg = Config(workspace_root=tmp_path)
    a = paths.make_session_dirs(cfg, "annotate", "s1")
    d = paths.make_session_dirs(cfg, "deck", "s1")
    assert paths.base_of(a) != paths.base_of(d)
    assert paths.base_of(a).parent.name == "annotate"
    assert paths.base_of(d).parent.name == "deck"


def test_every_expected_subdir_is_created(tmp_path):
    dirs = paths.make_session_dirs(Config(workspace_root=tmp_path), "annotate", "s1")
    assert set(dirs) == {"state_dir", "items_dir", "threads_dir",
                         "events_dir", "consumed_dir", "assets_dir"}
    for p in dirs.values():
        assert p.is_dir()


def test_marker_records_the_project_the_workspace_belongs_to(tmp_path):
    dirs = paths.make_session_dirs(Config(workspace_root=tmp_path), "deck", "s2")
    base = paths.base_of(dirs)
    paths.write_marker(base, "s2", "deck", "/home/x/proj")
    assert paths.read_marker(base) == {"sid": "s2", "kind": "deck", "cwd": "/home/x/proj"}


def test_missing_marker_reads_as_empty(tmp_path):
    dirs = paths.make_session_dirs(Config(workspace_root=tmp_path), "deck", "s3")
    assert paths.read_marker(paths.base_of(dirs)) == {}


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "Annotate!", "x" * 65])
def test_a_kind_that_could_escape_the_root_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        paths.make_session_dirs(Config(workspace_root=tmp_path), bad, "s1")


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "s id"])
def test_a_sid_that_could_escape_the_root_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        paths.make_session_dirs(Config(workspace_root=tmp_path), "annotate", bad)


def test_default_root_is_under_the_claude_directory():
    assert paths.workspace_root(Config()) == Path("~/.claude/webcompanion/workspaces").expanduser()
