from __future__ import annotations

from pathlib import Path

from webcompanion import items


def _session(call, kind="annotate", cwd="/p"):
    return call("POST", "/api/sessions", {"kind": kind, "cwd": cwd, "title": "T"})[1]


def test_put_then_get_one_item(call):
    s = _session(call)
    assert call("PUT", f"/s/{s['sid']}/items/b-1", {"text": "hello"})[0] == 200
    status, body = call("GET", f"/s/{s['sid']}/items/b-1")
    assert status == 200
    assert body["body"] == {"text": "hello"}
    assert body["version"] == 1


def test_patch_upserts_many_in_one_request(call):
    s = _session(call)
    call("PATCH", f"/s/{s['sid']}/items",
         {"items": {"b-1": {"t": "a"}, "b-2": {"t": "b"}}})
    _, all_items = call("GET", f"/s/{s['sid']}/items")
    assert set(all_items) == {"b-1", "b-2"}


def test_patch_with_replace_deletes_absent_anchors(call):
    s = _session(call)
    call("PATCH", f"/s/{s['sid']}/items", {"items": {"b-1": {}, "b-2": {}}})
    call("PATCH", f"/s/{s['sid']}/items", {"items": {"b-1": {}}, "replace": True})
    _, all_items = call("GET", f"/s/{s['sid']}/items")
    assert set(all_items) == {"b-1"}


def test_rewriting_one_item_bumps_only_its_version(call):
    s = _session(call)
    call("PATCH", f"/s/{s['sid']}/items", {"items": {"b-1": {"t": "a"}, "b-2": {"t": "b"}}})
    call("GET", f"/s/{s['sid']}/items")
    call("PUT", f"/s/{s['sid']}/items/b-2", {"t": "CHANGED"})
    _, all_items = call("GET", f"/s/{s['sid']}/items")
    assert all_items["b-1"]["version"] == 1
    assert all_items["b-2"]["version"] == 2


def test_delete_removes_an_item(call):
    s = _session(call)
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "a"})
    assert call("DELETE", f"/s/{s['sid']}/items/b-1")[0] == 200
    assert call("GET", f"/s/{s['sid']}/items/b-1")[0] == 404


def test_an_item_declaring_a_code_anchor_gets_it_resolved_on_read(tmp_path, call):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("one\ntwo\nthree\n")
    s = _session(call, cwd=str(repo))
    call("PUT", f"/s/{s['sid']}/items/b-1",
         {"text": "see this", "code": [{"file": "src/a.py", "line": 2, "snippet": "two"}]})
    _, body = call("GET", f"/s/{s['sid']}/items/b-1")
    assert body["code"][0]["status"] == "ok"
    assert any(l["text"] == "two" for l in body["code"][0]["lines"])


def test_editing_the_file_changes_the_next_read(tmp_path, call):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("one\ntwo\nthree\n")
    s = _session(call, cwd=str(repo))
    call("PUT", f"/s/{s['sid']}/items/b-1",
         {"code": [{"file": "src/a.py", "line": 2, "snippet": "two"}]})
    first = call("GET", f"/s/{s['sid']}/items/b-1")[1]["code"][0]
    # Edit a CONTEXT line, not the anchored line itself: rewriting the
    # anchored line would delete the snippet and flip status to "stale",
    # whose payload (anchors.py's _fail()) carries no "lines" key at all --
    # that shape is exercised by test_an_anchor_outside_the_session_cwd_is_
    # refused, not this one. This test's property is narrower: the anchor
    # still resolves, but the window it returns reflects the live file.
    (repo / "src" / "a.py").write_text("one\ntwo\nTHREE EDITED\n")
    second = call("GET", f"/s/{s['sid']}/items/b-1")[1]["code"][0]
    assert first["status"] == second["status"] == "ok"
    assert first["lines"] != second["lines"]


def test_an_anchor_outside_the_session_cwd_is_refused(tmp_path, call):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.txt").write_text("password")
    s = _session(call, cwd=str(repo))
    call("PUT", f"/s/{s['sid']}/items/b-1",
         {"code": [{"file": "../secret.txt", "line": 1, "snippet": "password"}]})
    _, body = call("GET", f"/s/{s['sid']}/items/b-1")
    # anchors.resolve_all's real status vocabulary is ok/moved/stale/refused/
    # missing (see anchors.py's own tests) -- "error" is not a status this
    # module ever produces, so the brief's assertion is corrected here.
    assert body["code"][0]["status"] == "refused"


def test_put_with_an_oversized_body_is_a_400(call):
    # items.put raises ValueError on an oversized body; the route must turn
    # that into a 400, never let it propagate into a 500.
    s = _session(call)
    huge = {"text": "x" * (items.MAX_BODY_BYTES + 1000)}
    assert call("PUT", f"/s/{s['sid']}/items/b-1", huge)[0] == 400


def test_submit_queues_an_event_and_returns_its_id(call):
    s = _session(call)
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "a"})
    status, body = call("POST", f"/s/{s['sid']}/api/submit",
                        {"anchor": "b-1", "text": "please change this"})
    assert status == 202
    assert body["event_id"]


def test_submit_on_an_unknown_anchor_is_a_400(call):
    s = _session(call)
    assert call("POST", f"/s/{s['sid']}/api/submit",
                {"anchor": "../escape", "text": "x"})[0] == 400


def test_poll_reports_item_and_thread_versions(call):
    s = _session(call)
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "a"})
    _, poll = call("GET", f"/s/{s['sid']}/poll")
    assert poll["items"]["b-1"] == 1
    assert "threads" in poll
    assert "watcher_seen_at" in poll


def test_open_in_editor_refuses_a_path_outside_every_session(tmp_path, call):
    repo = tmp_path / "repo"
    repo.mkdir()
    _session(call, cwd=str(repo))
    status, _ = call("POST", "/api/open",
                     {"file": str(tmp_path / "elsewhere.txt"), "line": 1})
    assert status == 403


def test_poll_reports_acked_event_ids(call, daemon):
    """The stream's `event-acked` frame is the only signal for an answer that
    changed nothing. A page that fell back to polling needs the same fact, or
    it stays locked after the answer."""
    s = _session(call)
    _, poll = call("GET", f"/s/{s['sid']}/poll")
    assert poll["acked"] == []
    dirs = daemon.registry.lookup(daemon.registry.resolve(s["sid"]))
    consumed = Path(dirs["consumed_dir"])
    consumed.mkdir(parents=True, exist_ok=True)
    (consumed / "evt-2.ack").write_text("")
    (consumed / "evt-1.ack").write_text("")
    (consumed / "evt-3.json").write_text("{}")
    _, poll = call("GET", f"/s/{s['sid']}/poll")
    assert poll["acked"] == ["evt-1", "evt-2"]
