"""`webcompanion ack` -- tell the watcher an event has been answered.

`watch` emits WEBCOMPANION_EVENT and then blocks for up to 30 minutes on
`<consumed_dir>/<event_id>.ack`. Nothing in the package wrote that file:
no command, no route. So out of the box every comment was emitted, waited
out, re-emitted twice more, and finally reported as WEBCOMPANION_DROPPED --
90 minutes after the user asked their question, having been answered the
whole time.

This is a FILESYSTEM operation, deliberately, and it does NOT preflight the
daemon. The queue, the heartbeat and the ack file are all files in the
session's own `state_dir`, written and read by processes on one host -- the
same assumption `watch` itself makes for everything except its one startup
health check. Making an ack depend on the daemon answering would mean a
daemon restart during a long answer silently re-emits an event that was
already handled, which is precisely the failure this command exists to end.

The session's directories are RESOLVED from the registry (shared with
`watch`), never computed and created: acking an unknown sid must fail, not
mkdir a workspace.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from webcompanion.commands.watch import UnknownSession, resolve_session_dirs

ACK_SUFFIX = ".ack"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion ack")
    p.add_argument("--sid", required=True,
                   help="the sid from the WEBCOMPANION_EVENT line")
    p.add_argument("--event-id", required=True, dest="event_id",
                   help="the event_id from the WEBCOMPANION_EVENT line")
    p.add_argument("--kind", default=None,
                   help="only needed to disambiguate a slug used by more "
                        "than one kind; a sid never needs it")
    return p


def write_ack(consumed_dir: Path, event_id: str) -> Path:
    """Create the ack file `watch` is blocked on. Empty by design -- the
    watcher tests for existence, never for content, so there is nothing to
    write and nothing that can be half-written."""
    consumed_dir = Path(consumed_dir)
    consumed_dir.mkdir(parents=True, exist_ok=True)
    path = consumed_dir / f"{event_id}{ACK_SUFFIX}"
    path.touch()
    return path


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    # An event_id becomes a filename. events.append mints it as
    # "<20 digits>-<pid>-<6 digits>", so anything with a separator in it is
    # not one of ours and must not be allowed to name a path.
    if "/" in args.event_id or args.event_id in ("", ".", ".."):
        print(f"webcompanion ack: not a valid event id: {args.event_id!r}",
              file=sys.stderr)
        return 1

    try:
        dirs = resolve_session_dirs(args.kind, args.sid)
    except UnknownSession:
        print(f"webcompanion ack: no such session: {args.sid}\n"
              f"  the daemon has no registered session by that id or slug.",
              file=sys.stderr)
        return 1

    path = write_ack(dirs["consumed_dir"], args.event_id)
    print(str(path))
    return 0
