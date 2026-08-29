"""The per-session SSE loop.

Two things differ from the version this replaces. There is no `extra` hook:
it took a Python callable so each skill could emit its own frames on the
shared loop, which a standalone daemon has no way to call. Every frame it
carried — walkthrough's steps-changed, dataflow's flow-changed — was really
"this changed to version N", so they collapse into item-changed and
document-changed.

And streams are counted and capped. Every connected browser tab and IDE
client parks a thread here for the life of the connection. That load used to
be spread over five processes that each shut down when idle; it is now one
process that never restarts, holding sessions from every project on the
machine.
"""
from __future__ import annotations

import json
import select
import socket
import threading
from pathlib import Path

MAX_CONCURRENT_STREAMS = 200
HEARTBEAT_SECONDS = 30

# How often the loop wakes to check the socket for a client-side close, even
# when nothing has changed. A graceful disconnect (the client closing its
# read side) shows up as the socket becoming readable-with-EOF; nothing
# about that involves a write, so it is invisible to `emit()`'s
# BrokenPipeError catch until the NEXT write happens. Without this poll, a
# client that vanishes between changes would hold its slot for up to
# HEARTBEAT_SECONDS.
POLL_SECONDS = 1.0

_open = 0
_open_lock = threading.Lock()


def open_stream_count() -> int:
    with _open_lock:
        return _open


class _Slot:
    """Reserve a stream slot, or refuse. Released on exit either way."""

    def __init__(self):
        self.acquired = False

    def __enter__(self):
        global _open
        with _open_lock:
            if _open >= MAX_CONCURRENT_STREAMS:
                return self
            _open += 1
            self.acquired = True
        return self

    def __exit__(self, *exc):
        global _open
        if self.acquired:
            with _open_lock:
                _open -= 1
        return False


def _client_gone(handler) -> bool:
    """True if the client has closed its side of the connection.

    A non-blocking peek: `select` reports the socket readable once the
    client's FIN has arrived, and a zero-length `recv` (MSG_PEEK, so nothing
    is consumed) on a readable socket means EOF, not "a request queued up
    behind this one" — clients never send anything after opening the
    stream.
    """
    try:
        sock = handler.connection
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def serve(handler, sid: str, dirs: dict, *, registry, is_terminal) -> None:
    from webcompanion import items as items_mod
    from webcompanion import threads as threads_mod

    with _Slot() as slot:
        if not slot.acquired:
            body = b"too many open streams"
            handler.send_response(503)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return

        state_dir = Path(dirs["state_dir"])
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()

        def emit(name: str, obj: dict) -> bool:
            try:
                handler.wfile.write(f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode())
                handler.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        if not emit("connected", {}):
            return

        last_items = items_mod.versions_of(dirs["items_dir"])
        last_threads = threads_mod.list_versions(dirs["threads_dir"])
        for anchor, version in last_items.items():
            if not emit("item-changed", {"anchor": anchor, "version": version}):
                return
        for anchor, version in last_threads.items():
            if not emit("thread-changed", {"anchor": anchor, "version": version}):
                return

        # Compared against a VALUE, not an edge. A change landing between the
        # snapshot above and the wait below is not lost.
        seen = registry.version(sid)
        idle_elapsed = 0.0
        while True:
            if _client_gone(handler):
                return

            now = registry.wait_for_change(sid, since=seen, timeout=POLL_SECONDS)
            woke = now > seen
            seen = now

            if is_terminal(state_dir):
                # Otherwise this loop re-reads every file every POLL_SECONDS
                # per connected client, forever.
                emit("session-ended", {})
                return

            if not woke:
                idle_elapsed += POLL_SECONDS
                if idle_elapsed < HEARTBEAT_SECONDS:
                    continue
                idle_elapsed = 0.0
                if not emit("heartbeat", {}):
                    return
                continue

            idle_elapsed = 0.0

            # versions_of()/list_versions() are called AT MOST ONCE per
            # iteration and the two dicts are reused for every comparison
            # below, including the deletion pass — each call reads every
            # item body from disk under an flock, and N open tabs already
            # means N of these per second.
            new_items = items_mod.versions_of(dirs["items_dir"])
            new_threads = threads_mod.list_versions(dirs["threads_dir"])

            # There is no dedicated item-deleted frame in the fixed
            # vocabulary (unlike threads, which have thread-deleted) — a
            # removed anchor is reported as item-changed with version 0, the
            # deletion pass built from the SAME two dicts fetched above.
            for anchor in set(last_items) - set(new_items):
                if not emit("item-changed", {"anchor": anchor, "version": 0}):
                    return
            for anchor, version in new_items.items():
                if last_items.get(anchor) != version:
                    if not emit("item-changed", {"anchor": anchor, "version": version}):
                        return
            for anchor in set(last_threads) - set(new_threads):
                if not emit("thread-deleted", {"anchor": anchor}):
                    return
            for anchor, version in new_threads.items():
                if last_threads.get(anchor) != version:
                    if not emit("thread-changed", {"anchor": anchor, "version": version}):
                        return

            last_items, last_threads = new_items, new_threads
