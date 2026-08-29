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


def test_append_atomic_write(tmp_path):
    events_dir = tmp_path / "events"
    events_dir.mkdir()
    eid = append(events_dir, {"x": 1})
    # No leftover .tmp files
    assert not list(events_dir.glob("*.tmp"))
    assert (events_dir / f"{eid}.json").exists()
