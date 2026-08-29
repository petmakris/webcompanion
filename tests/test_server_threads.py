"""The four thread routes.

Contract 1 already defined a Thread, already returned `threads: {anchor:
version}` from /poll, and already documented `thread-changed` and
`thread-deleted` SSE frames -- while nothing could read or append a thread.
Shipping 1.0.0 that way would have meant bumping the contract across four
artifacts to add what the contract said was there.
"""
from __future__ import annotations

import json
import time
import urllib.parse

import pytest

from webcompanion.gate import WRITE_TOKEN_HEADER


@pytest.fixture
def sid(call, tmp_path):
    _, created = call("POST", "/api/sessions",
                      {"kind": "annotate", "cwd": str(tmp_path), "title": "t"})
    return created["sid"]


def _q(anchor: str) -> str:
    return urllib.parse.quote(anchor, safe="")


def test_an_anchor_nobody_has_commented_on_reads_as_an_empty_thread(call, sid):
    """Not a 404. A client asks for the thread of every region it renders,
    and a 404 per un-commented region is noise, not information -- version 0
    with no messages IS the "nothing here yet" answer."""
    status, body = call("GET", f"/s/{sid}/threads/{_q('block-1')}")
    assert status == 200
    assert body == {"anchor": "block-1", "version": 0, "messages": []}


def test_appending_grows_the_thread_and_its_version(call, sid):
    for i, text in enumerate(["why this?", "because that"], start=1):
        status, body = call("POST", f"/s/{sid}/threads/{_q('block-1')}",
                            {"text": text, "role": "user" if i == 1 else "agent"})
        assert status == 200
        assert body == {"appended": True, "version": i}

    _, thread = call("GET", f"/s/{sid}/threads/{_q('block-1')}")
    assert [m["text"] for m in thread["messages"]] == ["why this?", "because that"]
    assert [m["role"] for m in thread["messages"]] == ["user", "agent"]
    assert all(isinstance(m["ts"], int) for m in thread["messages"])


def test_a_repeated_source_event_id_is_not_appended_twice(call, sid):
    """The watcher re-emits an unacked event up to three times. An agent
    answering the second emission must not double-post its reply."""
    body = {"text": "the answer", "source_event_id": "evt-1"}
    first = call("POST", f"/s/{sid}/threads/{_q('block-1')}", body)[1]
    second = call("POST", f"/s/{sid}/threads/{_q('block-1')}", body)[1]

    assert first == {"appended": True, "version": 1}
    assert second == {"appended": False, "version": 1}
    _, thread = call("GET", f"/s/{sid}/threads/{_q('block-1')}")
    assert len(thread["messages"]) == 1


def test_the_title_and_anchor_text_are_thread_level_not_message_level(call, sid):
    """`title` is the agent's headline for the IDE panel (last write wins);
    `anchor_text` records the anchored line once so a drifted annotation can
    be re-located later (first write wins). Neither belongs in the message
    list, so neither is stored there."""
    call("POST", f"/s/{sid}/threads/{_q('src/a.py:L:10')}",
         {"text": "one", "title": "Null check", "anchor_text": "if x:"})
    call("POST", f"/s/{sid}/threads/{_q('src/a.py:L:10')}",
         {"text": "two", "title": "Null check, revised", "anchor_text": "IGNORED"})

    _, thread = call("GET", f"/s/{sid}/threads/{_q('src/a.py:L:10')}")
    assert thread["title"] == "Null check, revised"
    assert thread["anchor_text"] == "if x:"
    assert all("title" not in m and "anchor_text" not in m
               for m in thread["messages"])


def test_the_snapshot_route_returns_every_thread_at_once(call, sid):
    call("POST", f"/s/{sid}/threads/{_q('a')}", {"text": "one"})
    call("POST", f"/s/{sid}/threads/{_q('b')}", {"text": "two"})

    status, body = call("GET", f"/s/{sid}/threads")
    assert status == 200
    assert set(body) == {"a", "b"}
    assert body["a"]["messages"][0]["text"] == "one"


def test_deleting_a_thread_removes_it_from_the_snapshot_and_from_poll(call, sid):
    call("POST", f"/s/{sid}/threads/{_q('a')}", {"text": "one"})
    assert call("GET", f"/s/{sid}/poll")[1]["threads"] == {"a": 1}

    status, body = call("POST", f"/s/{sid}/api/threads/delete", {"anchor": "a"})
    assert (status, body) == (200, {"deleted": True})
    assert call("GET", f"/s/{sid}/threads")[1] == {}
    assert call("GET", f"/s/{sid}/poll")[1]["threads"] == {}

    # Deleting again is not an error, it is just not a deletion.
    assert call("POST", f"/s/{sid}/api/threads/delete",
                {"anchor": "a"})[1] == {"deleted": False}


def test_appending_bumps_the_session_counter_so_sse_fires(daemon, call, sid):
    """The SSE loop compares against registry.version(sid). Without the
    bump, thread-changed frames only ever arrive on the 30s heartbeat's
    re-read -- which is not what `thread-changed` promises."""
    before = daemon.registry.version(sid)
    call("POST", f"/s/{sid}/threads/{_q('a')}", {"text": "one"})
    after_append = daemon.registry.version(sid)
    call("POST", f"/s/{sid}/api/threads/delete", {"anchor": "a"})

    assert after_append > before
    assert daemon.registry.version(sid) > after_append


# ── gating ───────────────────────────────────────────────────────────────

def test_reading_a_thread_is_not_owner_gated_but_writing_is(daemon, sid):
    """A shared read-only browser tab must be able to read threads; only a
    write needs the token. Exercised over a non-loopback-looking request by
    stripping loopback status the only way a test can: a forged Origin."""
    from tests.conftest import raw_call

    hostile = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    status, _ = raw_call(daemon, "GET", f"/s/{sid}/threads", headers=hostile)
    assert status == 200

    status, _ = raw_call(daemon, "POST", f"/s/{sid}/threads/a",
                         body={"text": "x"}, headers=hostile)
    assert status == 403

    status, _ = raw_call(daemon, "POST", f"/s/{sid}/api/threads/delete",
                         body={"anchor": "a"}, headers=hostile)
    assert status == 403


def test_a_write_with_the_token_is_allowed_from_a_non_owner_origin(daemon, sid):
    from tests.conftest import raw_call

    status, _ = raw_call(daemon, "POST", f"/s/{sid}/threads/a",
                         body={"text": "x"},
                         headers={WRITE_TOKEN_HEADER: daemon.cfg.token})
    assert status == 200


# ── validation ───────────────────────────────────────────────────────────

def test_an_empty_message_and_a_traversing_anchor_are_both_400(call, sid):
    assert call("POST", f"/s/{sid}/threads/{_q('a')}", {"text": "   "})[0] == 400
    assert call("POST", f"/s/{sid}/threads/{_q('a')}", {})[0] == 400
    assert call("POST", f"/s/{sid}/api/threads/delete", {})[0] == 400
    assert call("GET", f"/s/{sid}/threads/{_q('../../escape')}")[0] == 400


def test_an_unresolvable_sid_is_404_on_every_thread_route(call):
    assert call("GET", "/s/nope/threads")[0] == 404
    assert call("GET", "/s/nope/threads/a")[0] == 404
    assert call("POST", "/s/nope/threads/a", {"text": "x"})[0] == 404
    assert call("POST", "/s/nope/api/threads/delete", {"anchor": "a"})[0] == 404


def test_a_thread_anchor_may_look_like_a_path(call, sid):
    """interactive-review's anchors are `path:side:line`, which contain
    slashes once URL-decoded. The route's `(.+)` is what makes that work,
    and items.encode_anchor is what keeps it on one file."""
    anchor = "src/main/java/App.java:R:42"
    call("POST", f"/s/{sid}/threads/{_q(anchor)}", {"text": "here"})
    _, thread = call("GET", f"/s/{sid}/threads/{_q(anchor)}")
    assert thread["anchor"] == anchor
    assert thread["version"] == 1


# ── watcher_seen_at ──────────────────────────────────────────────────────

def test_poll_reports_the_watchers_heartbeat_instead_of_null_forever(
        daemon, call, sid):
    """`watch` writes state_dir/watcher_heartbeat on every poll; /poll never
    read it, so watcher_seen_at was permanently null and no client could
    tell a live session from an abandoned one."""
    from webcompanion.commands import watch

    assert call("GET", f"/s/{sid}/poll")[1]["watcher_seen_at"] is None

    state_dir = daemon.registry.lookup(sid)["state_dir"]
    watch.beat(state_dir)
    seen = call("GET", f"/s/{sid}/poll")[1]["watcher_seen_at"]
    assert isinstance(seen, int)
    assert abs(seen - time.time()) < 5


def test_an_unreadable_heartbeat_reads_as_no_watcher_not_a_500(daemon, call, sid):
    from pathlib import Path

    state_dir = Path(daemon.registry.lookup(sid)["state_dir"])
    (state_dir / "watcher_heartbeat").write_text("not a number")
    status, body = call("GET", f"/s/{sid}/poll")
    assert status == 200
    assert body["watcher_seen_at"] is None
