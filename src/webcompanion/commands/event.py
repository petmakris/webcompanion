"""`webcompanion event` -- print one queued or answered event in full.

`watch` prints each event's payload inline after its banner, but whoever
reads that stream may not see all of it. Claude Code's Monitor tool cuts a
long notification short and ends it with "...(truncated)", and a round of
several comments is exactly the payload that runs long. A consumer that
handled the inline copy acked an event whose other reactions it never saw,
and those comments were lost without a trace.

So a consumer treats the banner as a doorbell only: it takes the sid and
event_id from the banner line and reads the event itself with this command.
The file is read where it lies: in `events_dir` while the event waits for
its ack, in `consumed_dir` once the watcher has archived it.

Like `ack`, this is a filesystem read that needs no daemon, and the
session's directories are resolved from the registry, never computed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from webcompanion.commands import watch
from webcompanion.commands.watch import UnknownSession


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion event")
    p.add_argument("--sid", required=True,
                   help="the sid from the WEBCOMPANION_EVENT line")
    p.add_argument("--event-id", required=True, dest="event_id",
                   help="the event_id from the WEBCOMPANION_EVENT line")
    p.add_argument("--kind", default=None,
                   help="only needed to disambiguate a slug used by more "
                        "than one kind; a sid never needs it")
    return p


def read_event(events_dir: Path, consumed_dir: Path, event_id: str) -> str | None:
    """The event's stored JSON text, or None when neither directory has it."""
    for directory in (events_dir, consumed_dir):
        path = Path(directory) / f"{event_id}.json"
        try:
            return path.read_text()
        except FileNotFoundError:
            continue
    return None


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if "/" in args.event_id or args.event_id in ("", ".", ".."):
        print(f"webcompanion event: not a valid event id: {args.event_id!r}",
              file=sys.stderr)
        return 1

    try:
        dirs = watch.resolve_session_dirs(args.kind, args.sid)
    except UnknownSession:
        print(f"webcompanion event: no such session: {args.sid}\n"
              f"  the daemon has no registered session by that id or slug.",
              file=sys.stderr)
        return 1

    text = read_event(dirs["events_dir"], dirs["consumed_dir"], args.event_id)
    if text is None:
        print(f"webcompanion event: no event {args.event_id} in session {args.sid}",
              file=sys.stderr)
        return 1
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0
