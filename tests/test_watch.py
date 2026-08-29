"""Coverage for `commands/watch.py`, the Python port of `watcher.sh`.

Every test here drives `watch_loop` directly with tiny `poll_seconds` /
`ack_timeout_seconds` and a fake `sleep` rather than a real daemon and real
waits -- these behaviours (bounded re-emission, the drop banner, terminal
detection) are about the loop's own logic, not about anything the daemon
does, and a previous agent on this exact task stalled writing this file, so
every test gets a hard timeout: it runs in a background thread and the test
fails loudly instead of hanging CI if that thread does not finish in time.
"""
from __future__ import annotations

import io
import json
import threading
import time

import pytest

from webcompanion.commands import watch

HARD_TIMEOUT = 5.0


def run_with_hard_timeout(fn, timeout=HARD_TIMEOUT):
    """Run `fn` (no args) in a daemon thread; fail the test rather than hang
    forever if it does not return within `timeout` seconds."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - surfaced via box, not raised in the worker
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"{fn} did not finish within {timeout}s -- likely an infinite loop")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _write_event(events_dir, event_id: str, payload: dict) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{event_id}.json").write_text(json.dumps(payload))


# ── terminal detection is a FILE check ──────────────────────────────────

def test_terminal_state_reads_marker_files_not_memory(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    assert watch.terminal_state(state_dir) is None

    (state_dir / "finished").touch()
    assert watch.terminal_state(state_dir) == "finished"

    (state_dir / "cancelled").touch()
    # Both present is not a state that should arise, but the check must
    # still pick one deterministically rather than raise.
    assert watch.terminal_state(state_dir) == "cancelled"


def test_a_reaped_workspace_ends_the_watch_without_spinning(tmp_path):
    # state_dir is never created -- simulates retention or the stray sweep
    # having removed the workspace out from under a still-running watch.
    # events_dir and consumed_dir sit INSIDE state_dir, exactly as
    # paths._SUBDIRS lays them out: putting them elsewhere would have hidden
    # that watch_loop used to mkdir state_dir back into existence and never
    # reach this banner at all.
    state_dir = tmp_path / "gone" / "state"
    events_dir = state_dir / "events"
    consumed_dir = state_dir / "consumed"
    buf = io.StringIO()

    rc = run_with_hard_timeout(
        lambda: watch.watch_loop("annotate", "sid-1", state_dir, events_dir,
                                  consumed_dir, out=buf, poll_seconds=0.01,
                                  sleep=lambda s: None)
    )

    assert rc == 0
    assert buf.getvalue().strip() == "WEBCOMPANION_CANCELLED skill=annotate sid=sid-1"


# ── heartbeat is written by atomic rename, never observed empty ────────

def test_heartbeat_is_never_observed_empty(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    heartbeat = state_dir / watch.HEARTBEAT_FILE
    stop = threading.Event()
    empty_seen = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            watch.beat(state_dir)
            n += 1
            if n > 500:
                break

    def reader():
        while not stop.is_set():
            if heartbeat.exists():
                content = heartbeat.read_text()
                if content == "":
                    empty_seen.set()
                    return

    def run_both():
        w = threading.Thread(target=writer)
        r = threading.Thread(target=reader)
        r.start()
        w.start()
        w.join(HARD_TIMEOUT - 1)
        stop.set()
        r.join(HARD_TIMEOUT - 1)
        return not empty_seen.is_set()

    assert run_with_hard_timeout(run_both) is True


# ── keep beating while blocked on an ack, then archive on ack ──────────

def test_watch_loop_emits_one_banner_then_archives_on_ack(tmp_path):
    state_dir = tmp_path / "state"
    events_dir = tmp_path / "events"
    consumed_dir = tmp_path / "consumed"
    state_dir.mkdir()
    event_id = "00000000000000000001-1-000000"
    payload = {"anchor": "b-1", "text": "hello"}
    _write_event(events_dir, event_id, payload)
    ack_path = consumed_dir / f"{event_id}.ack"
    buf = io.StringIO()
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the ack arriving while watch is blocked waiting for
            # it -- the heartbeat must have already fired at least once by
            # now (asserted below via the heartbeat file existing).
            consumed_dir.mkdir(parents=True, exist_ok=True)
            ack_path.touch()
        elif calls["n"] >= 3:
            (state_dir / "finished").touch()

    rc = run_with_hard_timeout(
        lambda: watch.watch_loop("annotate", "sid-2", state_dir, events_dir,
                                  consumed_dir, out=buf, poll_seconds=0.001,
                                  ack_timeout_seconds=10.0, sleep=fake_sleep)
    )

    out = buf.getvalue()
    assert rc == 0
    assert out.count("WEBCOMPANION_EVENT") == 1
    assert f"WEBCOMPANION_EVENT skill=annotate sid=sid-2 event_id={event_id}" in out
    assert "---payload---" in out and "---end---" in out
    assert json.dumps(payload) in out
    assert out.strip().endswith("WEBCOMPANION_FINISHED skill=annotate sid=sid-2")
    # The event was archived, not left in the live queue nor duplicated.
    assert not (events_dir / f"{event_id}.json").exists()
    assert (consumed_dir / f"{event_id}.json").exists()
    # The heartbeat was written at least once (before the fake sleep fired).
    assert (state_dir / watch.HEARTBEAT_FILE).exists()


# ── ack timeout, bounded re-emission, then WEBCOMPANION_DROPPED ────────

def test_ack_timeout_re_emits_bounded_times_then_drops(tmp_path):
    state_dir = tmp_path / "state"
    events_dir = tmp_path / "events"
    consumed_dir = tmp_path / "consumed"
    state_dir.mkdir()
    event_id = "00000000000000000002-1-000000"
    _write_event(events_dir, event_id, {"anchor": "b-1", "text": "never acked"})
    buf = io.StringIO()
    calls = {"n": 0}

    def fake_sleep(_seconds):
        # Never acks. After the event has had its full 3 emits (3 ack-wait
        # iterations plus at least one idle wait), mark the session
        # finished so the loop terminates instead of spinning forever on
        # an empty, un-terminal queue.
        calls["n"] += 1
        if calls["n"] >= 6:
            (state_dir / "finished").touch()

    rc = run_with_hard_timeout(
        lambda: watch.watch_loop("annotate", "sid-3", state_dir, events_dir,
                                  consumed_dir, out=buf, poll_seconds=0.001,
                                  ack_timeout_seconds=0.001, max_emits=3,
                                  sleep=fake_sleep)
    )

    out = buf.getvalue()
    assert rc == 0
    assert out.count("WEBCOMPANION_EVENT") == 3
    assert f"WEBCOMPANION_DROPPED skill=annotate sid=sid-3 event_id={event_id}" in out
    # DROPPED must come after all three emits, and FINISHED after DROPPED --
    # giving up must still be reported before the watch itself ends.
    dropped_at = out.index("WEBCOMPANION_DROPPED")
    finished_at = out.index("WEBCOMPANION_FINISHED")
    last_event_at = out.rindex("WEBCOMPANION_EVENT")
    assert last_event_at < dropped_at < finished_at
    # Given up on -- archived, not left live, and its retry counter cleared.
    assert not (events_dir / f"{event_id}.json").exists()
    assert (consumed_dir / f"{event_id}.json").exists()
    assert not (consumed_dir / f"{event_id}.attempts").exists()


# ── events are consumed in filename (chronological) order ──────────────

def test_events_are_emitted_in_filename_order(tmp_path):
    state_dir = tmp_path / "state"
    events_dir = tmp_path / "events"
    consumed_dir = tmp_path / "consumed"
    state_dir.mkdir()
    ids = ["00000000000000000003-1-000000",
           "00000000000000000001-1-000000",
           "00000000000000000002-1-000000"]
    for eid in ids:
        _write_event(events_dir, eid, {"anchor": "b-1", "text": eid})
    buf = io.StringIO()
    consumed_dir.mkdir(parents=True, exist_ok=True)
    order = []

    def fake_sleep(_seconds):
        # Ack whichever event was most recently printed, then, once the
        # queue is empty, end the watch.
        remaining = sorted(events_dir.glob("*.json"))
        if not remaining and not order:
            return
        if len(order) < 3:
            printed = out_ref["buf"].getvalue()
            for eid in ids:
                if eid in printed and eid not in order:
                    order.append(eid)
                    (consumed_dir / f"{eid}.ack").touch()
                    break
        else:
            (state_dir / "finished").touch()

    out_ref = {"buf": buf}
    rc = run_with_hard_timeout(
        lambda: watch.watch_loop("annotate", "sid-4", state_dir, events_dir,
                                  consumed_dir, out=buf, poll_seconds=0.001,
                                  ack_timeout_seconds=10.0, sleep=fake_sleep)
    )

    assert rc == 0
    assert order == sorted(ids)  # chronological (lexical) order, not insertion order


# ── ack: the file watch_loop blocks on, and who writes it ────────────────

def test_an_ack_unblocks_the_loop_instead_of_re_emitting(tmp_path):
    """Before `webcompanion ack` existed, nothing in the package wrote this
    file. Every event was emitted, waited out, re-emitted twice, and then
    reported as WEBCOMPANION_DROPPED -- 90 minutes after the question, with
    the answer already given."""
    from webcompanion import events
    from webcompanion.commands import ack

    state_dir = tmp_path / "state"
    events_dir = state_dir / "events"
    consumed_dir = state_dir / "consumed"
    state_dir.mkdir()
    event_id = events.append(events_dir, {"anchor": "a", "text": "why?"})

    # The consuming skill answers and acks on the first tick after the
    # banner, then the session is finished so the loop has somewhere to end.
    ticks = []

    def sleep(_):
        ticks.append(1)
        if len(ticks) == 1:
            ack.write_ack(consumed_dir, event_id)
        elif len(ticks) > 2:
            (state_dir / "finished").touch()

    buf = io.StringIO()
    rc = run_with_hard_timeout(
        lambda: watch.watch_loop("annotate", "sid-1", state_dir, events_dir,
                                  consumed_dir, out=buf, poll_seconds=0.01,
                                  ack_timeout_seconds=1.0, sleep=sleep))

    assert rc == 0
    out = buf.getvalue()
    assert "WEBCOMPANION_FINISHED" in out
    assert out.count("WEBCOMPANION_EVENT") == 1
    assert "WEBCOMPANION_DROPPED" not in out
    assert (consumed_dir / f"{event_id}.json").is_file()  # archived


def test_ack_refuses_an_unknown_sid_and_a_path_shaped_event_id(tmp_path, capsys):
    from webcompanion.commands import ack

    assert ack.run(["--sid", "nope", "--event-id", "e1"]) == 1
    assert "no such session" in capsys.readouterr().err

    assert ack.run(["--sid", "nope", "--event-id", "../../etc/passwd"]) == 1
    assert "not a valid event id" in capsys.readouterr().err


def test_ack_is_a_registered_subcommand():
    from webcompanion import cli
    assert "ack" in cli.SUBCOMMANDS
