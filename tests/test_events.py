import json
import time
from pathlib import Path

from webcompanion.events import append


def test_append_creates_file(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    eid = append(events_dir, {"hello": "world"})
    files = list(events_dir.iterdir())
    assert len(files) == 1
    assert files[0].name == f"{eid}.json"
    assert json.loads(files[0].read_text()) == {"hello": "world"}


def test_append_monotonic_ordering(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    ids = [append(events_dir, {"i": i}) for i in range(5)]
    assert ids == sorted(ids), ids


def test_append_id_sorts_lexically_across_a_digit_length_boundary(tmp_path, monkeypatch):
    # A single test run's wall-clock nanosecond values never cross a digit-length
    # boundary, so `ids == sorted(ids)` over calls made moments apart cannot tell
    # a zero-padded id from an unpadded one -- both already sort the same way.
    # Forcing timestamps that cross 999 -> 1000 -> 10000 is what actually
    # exercises the :020d padding: unpadded, "1000" < "10000" < "999" lexically,
    # which is NOT numeric order.
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    timestamps = [999, 1000, 10000]
    it = iter(timestamps)
    monkeypatch.setattr("webcompanion.events.time.time_ns", lambda: next(it))

    ids = [append(events_dir, {"t": t}) for t in timestamps]

    assert ids == sorted(ids), ids
    # And directly: the timestamp segment is zero-padded to exactly 20 digits.
    for eid in ids:
        assert len(eid.split("-")[0]) == 20, eid


def test_append_atomic_write(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    eid = append(events_dir, {"x": 1})
    # No leftover .tmp files
    assert not list(events_dir.glob("*.tmp"))
    assert (events_dir / f"{eid}.json").exists()
