"""Event queue helpers for webcompanion.

Skills enqueue events by calling append(); the watcher script reads them in
chronological order and emits one stdout banner per event.  Atomicity is via
write_text_atomic (unique temp name + rename).

Ordering is by the event_id filename, which is a fixed-width wall-clock
timestamp (`time_ns`, zero-padded to 20 digits) plus a pid/counter suffix for
uniqueness. Fixed width makes the watcher's lexical `sort` chronological, and
wall-clock (not monotonic_ns) keeps ordering stable across a server restart.
"""
from __future__ import annotations

import itertools
import json
import os
import time
from pathlib import Path

from webcompanion.atomic import write_text_atomic

_counter = itertools.count()


def append(events_dir: Path, payload: dict) -> str:
    """Atomically enqueue an event.  Returns the event_id."""
    events_dir = Path(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    event_id = f"{time.time_ns():020d}-{os.getpid()}-{next(_counter):06d}"
    write_text_atomic(events_dir / f"{event_id}.json", json.dumps(payload))
    return event_id
