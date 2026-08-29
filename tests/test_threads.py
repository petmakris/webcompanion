from __future__ import annotations

import threading

from webcompanion import items, threads


def test_any_client_chosen_anchor_is_accepted(tmp_path):
    # The old grammar was interactive-review's: path:L|R:line. A slide id and
    # a graph node id are neither.
    for anchor in ["b-3", "slide-2/title", "node:UserService", "src/a.java:L:12",
                   threads.GENERAL_ANCHOR]:
        assert threads.valid_anchor(anchor), anchor


def test_an_anchor_that_could_escape_the_directory_is_rejected(tmp_path):
    for anchor in ["", "../escape", "a/../../b"]:
        assert not threads.valid_anchor(anchor)


def test_append_is_deduped_by_source_event_id(tmp_path):
    msg = {"role": "assistant", "text": "hi", "source_event_id": "e1"}
    assert threads.append_message(tmp_path, "b-1", msg) is True
    assert threads.append_message(tmp_path, "b-1", dict(msg)) is False
    assert len(threads.load(tmp_path, "b-1")["messages"]) == 1


def test_concurrent_appends_do_not_lose_messages(tmp_path):
    # The server worker handling /api/submit and the agent appending its reply
    # are genuinely concurrent; the flock is what makes this safe.
    def add(i):
        threads.append_message(tmp_path, "b-1",
                               {"role": "user", "text": str(i), "source_event_id": f"e{i}"})
    ts = [threading.Thread(target=add, args=(i,)) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(threads.load(tmp_path, "b-1")["messages"]) == 20


def test_lock_files_do_not_pollute_the_threads_directory(tmp_path):
    threads.append_message(tmp_path, "b-1", {"role": "user", "text": "x"})
    assert all(p.suffix == ".json" for p in tmp_path.iterdir())


def test_set_anchor_text_if_absent_is_first_write_wins(tmp_path):
    threads.set_anchor_text_if_absent(tmp_path, "b-1", "first")
    threads.set_anchor_text_if_absent(tmp_path, "b-1", "second")
    assert threads.load(tmp_path, "b-1")["anchor_text"] == "first"


def test_set_anchor_text_if_absent_ignores_empty_text(tmp_path):
    threads.set_anchor_text_if_absent(tmp_path, "b-1", "")
    assert threads.load(tmp_path, "b-1") == {"anchor": "b-1", "version": 0, "messages": []}


def test_delete_removes_the_thread_file(tmp_path):
    threads.append_message(tmp_path, "b-1", {"role": "user", "text": "x"})
    assert threads.delete(tmp_path, "b-1") is True
    assert threads.load(tmp_path, "b-1")["messages"] == []


def test_deleting_an_absent_thread_is_false_not_an_error(tmp_path):
    assert threads.delete(tmp_path, "nope") is False


def test_list_versions_reports_each_anchor(tmp_path):
    threads.append_message(tmp_path, "b-1", {"role": "user", "text": "x"})
    threads.append_message(tmp_path, "b-2", {"role": "user", "text": "y"})
    threads.append_message(tmp_path, "b-2", {"role": "user", "text": "z"})
    assert threads.list_versions(tmp_path) == {"b-1": 1, "b-2": 2}


def test_encode_anchor_is_shared_with_items_so_names_match(tmp_path):
    anchors = ["b-1", "slide-2/title", "node:UserService", "x" * 400, "a/" + "y" * 500]
    threads_dir = tmp_path / "threads"
    for anchor in anchors:
        threads.append_message(threads_dir, anchor, {"role": "user", "text": "x"})
        expected_name = f"{items.encode_anchor(anchor)}.json"
        assert (threads_dir / expected_name).exists(), anchor
