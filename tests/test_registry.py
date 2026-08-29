from __future__ import annotations

import threading
import time

from webcompanion import paths
from webcompanion.config import Config
from webcompanion.registry import Registry


def _mk(reg, cfg, kind, title, cwd="/proj", slug=""):
    sid = reg.make_sid()
    dirs = paths.make_session_dirs(cfg, kind, sid)
    return sid, reg.create(kind, sid, dirs, {"title": title}, cwd, slug)


def test_two_kinds_may_hold_the_same_slug(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    _, a = _mk(reg, cfg, "annotate", "My Plan")
    _, d = _mk(reg, cfg, "deck", "My Plan")
    assert a == "my-plan"
    assert d == "my-plan", "slugs are unique within a kind, not across kinds"


def test_same_slug_twice_in_one_kind_is_deduped(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    _, first = _mk(reg, cfg, "annotate", "My Plan")
    _, second = _mk(reg, cfg, "annotate", "My Plan")
    assert (first, second) == ("my-plan", "my-plan-2")


def test_resolve_requires_the_kind_to_disambiguate(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid_a, _ = _mk(reg, cfg, "annotate", "My Plan")
    sid_d, _ = _mk(reg, cfg, "deck", "My Plan")
    assert reg.resolve("my-plan", kind="annotate") == sid_a
    assert reg.resolve("my-plan", kind="deck") == sid_d
    assert reg.resolve(sid_a) == sid_a


def test_find_filters_by_cwd_and_kind(tmp_path):
    # Without the kind filter one daemon returns every kind for a cwd, and
    # the IntelliJ walkthrough panel latches an annotate session.
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid_w, _ = _mk(reg, cfg, "walkthrough", "W", cwd="/p")
    _mk(reg, cfg, "annotate", "A", cwd="/p")
    _mk(reg, cfg, "walkthrough", "W2", cwd="/other")
    found = reg.find(cwd="/p", kind="walkthrough")
    assert [sid for sid, _ in found] == [sid_w]


def test_meta_records_the_kind(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid, _ = _mk(reg, cfg, "deck", "D")
    assert reg.get_meta(sid)["kind"] == "deck"


def test_persist_and_rehydrate_survive_a_restart(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid, slug = _mk(reg, cfg, "annotate", "Keep Me")
    reg.persist()
    fresh = Registry(tmp_path / "state")
    fresh.rehydrate()
    assert fresh.resolve(slug, kind="annotate") == sid
    assert fresh.get_meta(sid)["kind"] == "annotate"


def test_rehydrate_drops_rows_whose_directories_are_gone(tmp_path):
    import shutil
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid, _ = _mk(reg, cfg, "annotate", "Doomed")
    reg.persist()
    shutil.rmtree(paths.base_of(reg.lookup(sid)))
    fresh = Registry(tmp_path / "state")
    fresh.rehydrate()
    assert fresh.lookup(sid) is None


def test_change_counter_is_monotonic_not_an_edge(tmp_path):
    reg = Registry(tmp_path / "state")
    assert reg.version("s") == 0
    assert reg.note_change("s") == 1
    assert reg.note_change("s") == 2
    assert reg.version("s") == 2


def test_a_change_between_read_and_wait_is_not_lost(tmp_path):
    # The old Event.set()/clear() pair could drop exactly this: the change
    # lands after the caller reads its snapshot but before it blocks.
    reg = Registry(tmp_path / "state")
    seen = reg.version("s")
    reg.note_change("s")
    assert reg.wait_for_change("s", since=seen, timeout=0.01) == 1


def test_wait_returns_the_same_version_on_timeout(tmp_path):
    reg = Registry(tmp_path / "state")
    started = time.monotonic()
    assert reg.wait_for_change("s", since=0, timeout=0.05) == 0
    assert time.monotonic() - started >= 0.04


def test_a_waiting_thread_wakes_on_change(tmp_path):
    reg = Registry(tmp_path / "state")
    out = []
    t = threading.Thread(target=lambda: out.append(reg.wait_for_change("s", 0, 5.0)))
    t.start()
    time.sleep(0.05)
    reg.note_change("s")
    t.join(timeout=2)
    assert out == [1]


def test_six_concurrent_creates_get_distinct_slugs(tmp_path):
    # Pick-and-insert must happen inside one lock acquisition. Computing a
    # free slug, releasing the lock, then registering is check-then-act: six
    # concurrent creates with the same title could all compute "my-plan"
    # before any of them registers it.
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sids = [reg.make_sid() for _ in range(6)]
    dirs = {sid: paths.make_session_dirs(cfg, "annotate", sid) for sid in sids}
    slugs: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def worker(sid):
        barrier.wait()
        slug = reg.create("annotate", sid, dirs[sid], {"title": "My Plan"}, "/proj")
        with lock:
            slugs.append(slug)

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(slugs) == 6
    assert len(set(slugs)) == 6, "concurrent creates with the same title must not collide"


def test_unregister_frees_the_slug_and_the_counter(tmp_path):
    cfg = Config(workspace_root=tmp_path / "ws")
    reg = Registry(tmp_path / "state")
    sid, slug = _mk(reg, cfg, "annotate", "Gone")
    reg.note_change(sid)
    reg.unregister(sid)
    assert reg.resolve(slug, kind="annotate") is None
    assert sid not in reg._counters, "a never-restarting process must not leak per-session state"
