from __future__ import annotations

import json
import signal
import threading
import urllib.request

import pytest

from webcompanion import stream


@pytest.fixture(autouse=True)
def _hard_timeout():
    """A per-test wall-clock ceiling using stdlib signal.alarm.

    Threaded SSE tests can hang forever (a background reader thread blocked
    on a socket read that never returns). There is no pytest-timeout plugin
    in this environment and the project takes no test dependencies either,
    so this is the stdlib substitute: SIGALRM fires in the main thread
    (where the test body runs) and turns a hang into a clean failure instead
    of a wedged test run.
    """
    def _handler(signum, frame):
        raise TimeoutError("test exceeded its 10s hard timeout")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(10)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _read_frames(url, count, timeout=5, sync_after=None, sync_event=None):
    """Read `count` SSE frames as (event, data) pairs.

    If `sync_event` is given, it is set once `sync_after` frames have been
    parsed (default 1, i.e. just the "connected" frame) — so a caller can
    block until the stream has reached a known point instead of guessing
    with a sleep.
    """
    frames, name = [], None
    threshold = sync_after if sync_after is not None else 1
    with urllib.request.urlopen(url, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                frames.append((name, json.loads(line[6:])))
                name = None
                if sync_event is not None and len(frames) == threshold:
                    sync_event.set()
                if len(frames) >= count:
                    return frames
    return frames


def test_the_stream_opens_with_a_connected_frame(daemon, call):
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    frames = _read_frames(f"{daemon.url}/s/{s['sid']}/stream", 1)
    assert frames[0][0] == "connected"


def test_an_item_write_emits_item_changed_with_the_new_version(daemon, call):
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "a"})
    got = []
    # serve() runs, on one thread with no I/O wait in between: emit
    # connected -> snapshot last_items/last_threads -> emit the initial
    # item-changed echo for the pre-existing anchor -> capture
    # `seen = registry.version(sid)`. Waiting only for "connected" (frame
    # 1) left a window open on a fast loopback round trip: the second PUT's
    # note_change() could land between that snapshot and the `seen` read,
    # bumping the counter to a value `seen` already captures, so the wait
    # loop believes nothing happened after `seen` and the update is
    # dropped for good (reproduced deterministically once the 0.3s sleep
    # was removed). Waiting for frame 2 -- the item-changed echo of b-1's
    # existing version -- pins the handoff to just after that snapshot,
    # collapsing the race to a single non-yielding statement.
    sync = threading.Event()
    t = threading.Thread(
        target=lambda: got.extend(_read_frames(
            f"{daemon.url}/s/{s['sid']}/stream", 3, sync_after=2, sync_event=sync)))
    t.start()
    assert sync.wait(timeout=5), "stream never echoed the pre-existing item"
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "CHANGED"})
    t.join(timeout=5)
    changed = [f for f in got if f[0] == "item-changed"]
    assert changed, f"expected item-changed, got {[f[0] for f in got]}"
    assert changed[-1][1] == {"anchor": "b-1", "version": 2}


def test_there_is_no_skill_specific_frame_hook():
    import inspect
    sig = inspect.signature(stream.serve)
    assert "extra" not in sig.parameters, (
        "the old extra= hook took a Python callable so each skill could add "
        "its own frames; a standalone daemon cannot call into client code")
    assert not any(
        p.annotation in ("Callable", "callable")
        or (p.default is not inspect.Parameter.empty and callable(p.default))
        for p in sig.parameters.values()
    ), "serve must not accept a callable parameter of any name"


def test_only_the_documented_frames_are_emitted():
    import inspect
    src = inspect.getsource(stream)
    emitted = set(__import__("re").findall(r'emit\("([a-z-]+)"', src))
    assert emitted <= {"connected", "item-changed", "document-changed",
                       "thread-changed", "thread-deleted", "event-acked",
                       "heartbeat", "session-ended"}


def test_the_opening_snapshot_marks_its_frames_initial(daemon, call):
    # A client that has just fetched current state does not need to
    # re-render on the stream's opening echo of every existing anchor --
    # see core.js's onDelta contract. Only the snapshot loop sets this; a
    # later item-changed from a real write (test above) has no such key.
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    call("PUT", f"/s/{s['sid']}/items/b-1", {"t": "a"})
    frames = _read_frames(f"{daemon.url}/s/{s['sid']}/stream", 2)
    snapshot = [f for f in frames if f[0] == "item-changed"]
    assert snapshot, f"expected an item-changed snapshot frame, got {[f[0] for f in frames]}"
    assert snapshot[0][1].get("initial") is True


def test_a_finished_session_ends_the_stream(daemon, call):
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    call("POST", f"/s/{s['sid']}/api/finish")
    frames = _read_frames(f"{daemon.url}/s/{s['sid']}/stream", 2)
    assert any(f[0] == "session-ended" for f in frames)


def test_the_stream_count_returns_to_zero_after_a_client_disconnects(daemon, call):
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    _read_frames(f"{daemon.url}/s/{s['sid']}/stream", 1)
    import time
    for _ in range(50):
        if stream.open_stream_count() == 0:
            break
        time.sleep(0.1)
    assert stream.open_stream_count() == 0


def test_the_cap_refuses_a_stream_rather_than_exhausting_threads(daemon, call, monkeypatch):
    monkeypatch.setattr(stream, "MAX_CONCURRENT_STREAMS", 0)
    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    assert call("GET", f"/s/{s['sid']}/stream")[0] == 503


def test_client_gone_treats_a_closed_fd_as_disconnected_not_an_error():
    """select.select validates the fd's sign before the syscall and raises
    ValueError (not OSError) for fileno() == -1 -- the shape a
    server-side-already-closed socket takes during shutdown. That must be
    treated as "client gone" like any other closed-socket error, not escape
    into the request thread."""
    class _ClosedConnection:
        def fileno(self):
            return -1

    class _FakeHandler:
        connection = _ClosedConnection()

    assert stream._client_gone(_FakeHandler()) is True


def test_an_ack_is_reported_even_when_nothing_changed(daemon, call):
    """`event-acked` is the only frame reporting something other than content
    moving, and that is the whole reason it exists.

    Acks are files written by `webcompanion ack` without touching the daemon,
    so they bump no version and never wake the stream's registry wait. A
    renderer that locks its page while a comment is in flight has no other
    evidence the comment was answered: "answered, nothing needed changing"
    moves no item version and is otherwise indistinguishable from "still
    working". A page stuck on that was the bug this frame closes.

    Deterministic because the stream snapshots the consumed dir BEFORE it
    emits `connected` — once the reader has that frame, any later ack is new.
    """
    from webcompanion.commands.ack import write_ack
    from webcompanion import paths as paths_mod

    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    sid = s["sid"]
    dirs = paths_mod.make_session_dirs(daemon.cfg, "annotate", sid)

    connected = threading.Event()
    frames = []

    def reader():
        frames.extend(_read_frames(f"{daemon.url}/s/{sid}/stream", 2,
                                   timeout=15, sync_event=connected))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    assert connected.wait(10), "stream never sent its connected frame"

    # No item is written at any point — the ack is the only thing that happens.
    write_ack(dirs["consumed_dir"], "01700000000000000000-1-000000")
    t.join(timeout=15)

    acked = [data for name, data in frames if name == "event-acked"]
    assert acked, f"no event-acked frame; got {[n for n, _ in frames]}"
    assert acked[0]["event_id"] == "01700000000000000000-1-000000"


def test_an_ack_written_before_the_stream_opened_is_not_replayed(daemon, call):
    """The scan is a set difference against a snapshot, not a listing echoed
    every tick.

    Without the `last_acks` bookkeeping every connected tab would receive one
    `event-acked` per poll interval, forever, for every ack ever written. The
    session here already has an ack on disk before the stream opens; the only
    frame the reader may see is the heartbeat.
    """
    from webcompanion.commands.ack import write_ack
    from webcompanion import paths as paths_mod

    s = call("POST", "/api/sessions", {"kind": "annotate", "cwd": "/p", "title": "T"})[1]
    sid = s["sid"]
    dirs = paths_mod.make_session_dirs(daemon.cfg, "annotate", sid)
    write_ack(dirs["consumed_dir"], "01700000000000000000-1-000001")

    frames = []

    def reader():
        # Asks for more frames than can legitimately arrive, so the read runs
        # to its timeout and any replay would show up.
        frames.extend(_read_frames(f"{daemon.url}/s/{sid}/stream", 5, timeout=4))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout=12)

    acked = [n for n, _ in frames if n == "event-acked"]
    assert not acked, f"a pre-existing ack was replayed {len(acked)} times"
