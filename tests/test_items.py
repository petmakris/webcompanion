from __future__ import annotations

import pytest

from webcompanion import items


def test_put_then_load_roundtrips(tmp_path):
    items.put(tmp_path, "b-1", {"text": "hello"})
    assert items.load_one(tmp_path, "b-1") == {"text": "hello"}


def test_snapshot_reports_bodies_with_derived_versions(tmp_path):
    items.put(tmp_path, "b-1", {"text": "one"})
    items.put(tmp_path, "b-2", {"text": "two"})
    snap = items.snapshot(tmp_path)
    assert snap["b-1"] == {"body": {"text": "one"}, "version": 1}
    assert snap["b-2"]["version"] == 1


def test_rewriting_one_item_bumps_only_its_version(tmp_path):
    items.put_many(tmp_path, {"b-1": {"t": "a"}, "b-2": {"t": "b"}})
    items.snapshot(tmp_path)
    items.put(tmp_path, "b-2", {"t": "CHANGED"})
    snap = items.snapshot(tmp_path)
    assert (snap["b-1"]["version"], snap["b-2"]["version"]) == (1, 2)


def test_delete_removes_the_item_and_prunes_its_chain(tmp_path):
    items.put(tmp_path, "b-1", {"t": "a"})
    items.snapshot(tmp_path)
    assert items.delete(tmp_path, "b-1") is True
    assert items.load_one(tmp_path, "b-1") is None
    assert items.snapshot(tmp_path) == {}
    items.put(tmp_path, "b-1", {"t": "fresh"})
    assert items.snapshot(tmp_path)["b-1"]["version"] == 1


def test_deleting_an_absent_item_is_false_not_an_error(tmp_path):
    assert items.delete(tmp_path, "nope") is False


def test_put_many_replaces_the_whole_document(tmp_path):
    items.put_many(tmp_path, {"b-1": {"t": "a"}, "b-2": {"t": "b"}})
    items.put_many(tmp_path, {"b-1": {"t": "a"}}, replace=True)
    assert set(items.load_all(tmp_path)) == {"b-1"}


def test_put_many_without_replace_is_an_upsert(tmp_path):
    items.put_many(tmp_path, {"b-1": {"t": "a"}})
    items.put_many(tmp_path, {"b-2": {"t": "b"}})
    assert set(items.load_all(tmp_path)) == {"b-1", "b-2"}


@pytest.mark.parametrize("anchor", ["", "../escape", "a/../b", "\x00null"])
def test_an_anchor_that_could_escape_the_directory_is_rejected(tmp_path, anchor):
    assert items.valid_anchor(anchor) is False
    with pytest.raises(ValueError):
        items.put(tmp_path, anchor, {"t": "x"})


def test_two_long_anchors_sharing_a_prefix_do_not_collide(tmp_path):
    # Both anchors share a common prefix longer than the encoding's length
    # cap and differ only near the end. A truncating implementation would
    # chop both down to the same shared prefix and collide onto one file;
    # only a real hash of the full anchor keeps them distinct.
    prefix = "src/" + "x" * 400
    anchor_a = prefix + "/alpha.java:L:1"
    anchor_b = prefix + "/beta.java:L:1"
    items.put(tmp_path, anchor_a, {"t": "alpha-body"})
    items.put(tmp_path, anchor_b, {"t": "beta-body"})
    assert items.load_one(tmp_path, anchor_a) == {"t": "alpha-body"}
    assert items.load_one(tmp_path, anchor_b) == {"t": "beta-body"}
    assert len(items.load_all(tmp_path)) == 2
    assert all(len(p.name) <= 210 for p in tmp_path.iterdir())


def test_an_oversized_body_is_rejected(tmp_path):
    huge = {"t": "x" * (items.MAX_BODY_BYTES + 1)}
    with pytest.raises(ValueError):
        items.put(tmp_path, "b-1", huge)


def test_a_corrupt_item_file_is_skipped_not_fatal(tmp_path):
    items.put(tmp_path, "b-1", {"t": "good"})
    (tmp_path / "corrupt.json").write_text("{not json")
    assert items.load_all(tmp_path) == {"b-1": {"t": "good"}}


def test_a_stray_lock_file_does_not_appear_as_an_item(tmp_path):
    items.put(tmp_path, "b-1", {"t": "good"})
    items.snapshot(tmp_path)  # creates the chain file, and its .lock sidecar
    (tmp_path / f"{items.CHAIN_FILE}.lock").write_text("")
    assert items.load_all(tmp_path) == {"b-1": {"t": "good"}}


def test_put_many_with_one_bad_anchor_writes_nothing(tmp_path):
    with pytest.raises(ValueError):
        items.put_many(tmp_path, {"b-1": {"t": "good"}, "../escape": {"t": "bad"}})
    assert items.load_all(tmp_path) == {}


def test_put_many_replace_with_one_bad_anchor_deletes_nothing(tmp_path):
    items.put_many(tmp_path, {"b-1": {"t": "old"}})
    with pytest.raises(ValueError):
        items.put_many(tmp_path, {"b-2": {"t": "good"}, "": {"t": "bad"}}, replace=True)
    assert items.load_all(tmp_path) == {"b-1": {"t": "old"}}
