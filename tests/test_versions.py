from __future__ import annotations

import json

from webcompanion.versions import body_hash, derive_versions


def test_a_new_anchor_starts_at_version_one(tmp_path):
    v = derive_versions(tmp_path / "v.json", {"b-1": {"text": "hello"}})
    assert v == {"b-1": 1}


def test_unchanged_content_does_not_bump(tmp_path):
    p = tmp_path / "v.json"
    derive_versions(p, {"b-1": {"text": "hello"}})
    assert derive_versions(p, {"b-1": {"text": "hello"}}) == {"b-1": 1}


def test_changed_content_bumps_by_one(tmp_path):
    p = tmp_path / "v.json"
    derive_versions(p, {"b-1": {"text": "hello"}})
    assert derive_versions(p, {"b-1": {"text": "goodbye"}}) == {"b-1": 2}


def test_key_order_is_not_a_content_change(tmp_path):
    p = tmp_path / "v.json"
    derive_versions(p, {"b-1": {"a": 1, "b": 2}})
    assert derive_versions(p, {"b-1": {"b": 2, "a": 1}}) == {"b-1": 1}


def test_only_the_edited_anchor_bumps(tmp_path):
    p = tmp_path / "v.json"
    derive_versions(p, {"b-1": {"t": "a"}, "b-2": {"t": "b"}})
    out = derive_versions(p, {"b-1": {"t": "a"}, "b-2": {"t": "CHANGED"}})
    assert out == {"b-1": 1, "b-2": 2}


def test_a_removed_anchor_is_pruned_so_a_reused_id_starts_fresh(tmp_path):
    # Anchors can be reminted. If a deleted anchor's chain lingered, a new
    # item reusing that id would inherit a stale version — and if its content
    # happened to hash-match the old tail it would be reported unchanged.
    p = tmp_path / "v.json"
    derive_versions(p, {"b-1": {"t": "old"}})
    derive_versions(p, {"b-1": {"t": "old2"}})
    derive_versions(p, {})
    assert derive_versions(p, {"b-1": {"t": "brand new"}}) == {"b-1": 1}


def test_a_corrupt_chain_file_is_treated_as_absent(tmp_path):
    p = tmp_path / "v.json"
    p.write_text("{not json")
    assert derive_versions(p, {"b-1": {"t": "x"}}) == {"b-1": 1}


def test_concurrent_writers_converge(tmp_path):
    import threading
    p = tmp_path / "v.json"
    bodies = {"b-1": {"t": "same"}}
    threads = [threading.Thread(target=derive_versions, args=(p, bodies)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert derive_versions(p, bodies) == {"b-1": 1}
    assert len(json.loads(p.read_text())["b-1"]) == 1


def test_hash_is_stable_across_calls():
    assert body_hash({"a": 1}) == body_hash({"a": 1})
    assert body_hash({"a": 1}) != body_hash({"a": 2})


def _derive_versions_unlocked(chain_path, bodies):
    """Unlocked variant to reproduce the read-compute-write race.

    This is used to prove the test harness can detect the bug when
    synchronization is missing. Includes a sleep to widen the race window.
    """
    import time
    from webcompanion.versions import _load_chain, body_hash
    from webcompanion.atomic import write_text_atomic

    chain_path = __import__("pathlib").Path(chain_path)
    chain = _load_chain(chain_path)
    changed = False

    for stale in [k for k in chain if k not in bodies]:
        del chain[stale]
        changed = True

    # Widen the race window: give other threads time to read the same
    # base state before this thread computes and writes
    time.sleep(0.005)

    for anchor, body in bodies.items():
        if not isinstance(anchor, str):
            continue
        h = body_hash(body if isinstance(body, dict) else {"_": body})
        history = chain.setdefault(anchor, [])
        if not history or history[-1] != h:
            history.append(h)
            changed = True

    if changed:
        write_text_atomic(chain_path, __import__("json").dumps(chain, indent=2))

    return {a: len(chain[a]) for a in bodies if isinstance(a, str)}


def test_concurrent_different_anchors_no_lost_updates(tmp_path):
    """Eight threads, each bumping a different anchor.

    This targets the read-modify-write race that the lock fixes:
    if two threads read the same base chain, compute independently,
    and write back, one of the updates is lost. The test ensures
    all eight bumps are recorded in the final chain.
    """
    import threading
    p = tmp_path / "v.json"

    # Initialize with 8 anchors at version 1 (all with v=0)
    initial = {f"b-{i}": {"v": 0} for i in range(8)}
    derive_versions(p, initial)

    # Track which anchors each thread bumped
    bumped = {}

    # Each thread bumps its own anchor while preserving others
    def bump_anchor(i):
        bodies = {f"b-{j}": {"v": i+1 if i == j else 0} for j in range(8)}
        derive_versions(p, bodies)
        bumped[i] = f"b-{i}"

    threads = [threading.Thread(target=bump_anchor, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Verify all 8 anchors exist and have at least 2 entries (init + bump)
    chain_data = json.loads((p).read_text())
    for i in range(8):
        anchor = f"b-{i}"
        assert anchor in chain_data, f"Anchor {anchor} missing from chain"
        assert len(chain_data[anchor]) >= 2, f"Anchor {anchor} has {len(chain_data[anchor])} entries, expected >= 2"


def test_unlocked_variant_loses_updates(tmp_path):
    """Prove the harness can see the lost-update bug.

    Run the same 8-thread scenario with the unlocked variant.
    If this passes without lost updates, the harness is not creating
    enough contention and the locked test is meaningless.
    """
    import threading
    p = tmp_path / "v.json"

    # Initialize with 8 anchors
    initial = {f"b-{i}": {"v": 0} for i in range(8)}
    _derive_versions_unlocked(p, initial)

    # Each thread bumps its own anchor while preserving others (same harness as locked test)
    def bump_anchor(i):
        bodies = {f"b-{j}": {"v": i+1 if i == j else 0} for j in range(8)}
        _derive_versions_unlocked(p, bodies)

    threads = [threading.Thread(target=bump_anchor, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # This SHOULD lose some updates (some anchors < 2 entries)
    # If it doesn't, the harness is not hitting the race
    chain_data = json.loads((p).read_text())
    incomplete = [i for i in range(8) if len(chain_data.get(f"b-{i}", [])) < 2]
    assert incomplete, f"Harness does not create contention; no lost updates observed: {chain_data}"
