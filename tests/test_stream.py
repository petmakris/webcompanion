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


def _read_frames(url, count, timeout=5):
    """Read `count` SSE frames as (event, data) pairs."""
    frames, name = [], None
    with urllib.request.urlopen(url, timeout=timeout) as r:
        for raw in r:
            line = raw.decode().rstrip("\n")
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: ") and name:
                frames.append((name, json.loads(line[6:])))
                name = None
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
    t = threading.Thread(
        target=lambda: got.extend(_read_frames(f"{daemon.url}/s/{s['sid']}/stream", 3)))
    t.start()
    import time
    time.sleep(0.3)
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
                       "thread-changed", "thread-deleted", "heartbeat",
                       "session-ended"}


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
