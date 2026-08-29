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
