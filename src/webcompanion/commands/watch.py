"""`webcompanion watch` -- the successor to `watcher.sh`.

Every behaviour below was earned by a specific failure in the shell script
this replaces, and all of it survives the port:

  * Terminal detection is a FILE check on `state_dir/finished` and
    `state_dir/cancelled`. `watch` is a separate OS process from the daemon
    and cannot see its in-memory state -- the same reason `stream.py` and
    `server.py`'s `_is_terminal` read these as files rather than a dict.
  * Heartbeats are written by atomic rename, never truncate-then-fill.
    `date > file` (or a plain `open(...).write()`) truncates the target
    BEFORE writing the new value, so a reader can catch the file empty for
    the milliseconds between truncate and fill. Measured at ~0.6% of reads
    in the shell version; a reader that lands there sees "no heartbeat ever
    written", which a poller reads as a dead session and latches read-only
    on one that is very much alive. Writing to a private temp file and
    `os.replace`-ing it over the target means every reader sees either the
    old beat or the new one, never neither.
  * The heartbeat keeps beating while blocked waiting for an event's ack --
    otherwise `watcher_seen_at` goes stale for up to the whole ack timeout
    and a live session looks dead.
  * An unacked event is re-emitted, bounded to `max_emits` attempts (default
    3), so one perpetually-unanswered event cannot wedge the
    serially-processed queue behind it forever. Giving up prints
    `WEBCOMPANION_DROPPED` loudly -- the user must be told their question
    was dropped, not left watching a spinner vanish.
  * Events are consumed in filename order: `events.append` names each file
    with a fixed-width, zero-padded `time_ns`, so a lexical sort is a
    chronological one.
  * A reaped workspace (its `state_dir` gone -- retention or a stray sweep
    got to it) ends the watch with `WEBCOMPANION_CANCELLED` rather than
    spinning forever writing heartbeats into a void.
  * The session's directories are RESOLVED from the registry, never created.
    `paths.make_session_dirs` mkdirs, so calling it here meant `watch --sid
    <typo>` silently built a six-directory workspace and then blocked
    forever printing nothing -- and a watch started after a workspace was
    reaped recreated `state_dir`, which is the one condition the
    WEBCOMPANION_CANCELLED banner above tests for. An unknown sid must fail
    loudly instead.

`watch` never talks to the daemon over HTTP for any of this: the queue and
the terminal markers are files in the session's own `state_dir`, computed
the same way the daemon computes them -- via `config.load()` and
`paths.make_session_dirs`, run on the SAME host as the daemon, exactly the
assumption `watcher.sh` always made. The one HTTP call `run()` makes is the
health check every command makes before doing anything, because the CLI
never starts the daemon and never silently spins against one that is not
there.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from webcompanion import paths
from webcompanion.commands._common import client_from_config, preflight
from webcompanion.registry import Registry

FINISHED_MARKER = "finished"
CANCELLED_MARKER = "cancelled"
HEARTBEAT_FILE = "watcher_heartbeat"

DEFAULT_MAX_EMITS = 3
# 1800 * 1s poll == 30 minutes, the same ack window watcher.sh used.
DEFAULT_ACK_TIMEOUT_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 1.0


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so no reader ever observes it empty -- see the
    module docstring. The temp name carries the pid because several
    processes may share one state_dir."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def beat(state_dir: Path) -> None:
    atomic_write(Path(state_dir) / HEARTBEAT_FILE, f"{int(time.time())}\n")


def terminal_state(state_dir: Path) -> str | None:
    """"cancelled", "finished", or None. Checked in this order because a
    session marked both (should never happen, but a filesystem check must
    still pick one) is reported as cancelled -- the more conservative of
    the two banners to end a watch on."""
    state_dir = Path(state_dir)
    if (state_dir / CANCELLED_MARKER).exists():
        return "cancelled"
    if (state_dir / FINISHED_MARKER).exists():
        return "finished"
    return None


def _next_event(events_dir: Path) -> Path | None:
    """The oldest un-picked-up event, or None. Fixed-width event-id
    filenames sort lexically in the same order they were created."""
    events_dir = Path(events_dir)
    if not events_dir.is_dir():
        return None
    files = sorted(events_dir.glob("*.json"))
    return files[0] if files else None


def _ack_path(consumed_dir: Path, event_id: str) -> Path:
    return Path(consumed_dir) / f"{event_id}.ack"


def _attempts_path(consumed_dir: Path, event_id: str) -> Path:
    return Path(consumed_dir) / f"{event_id}.attempts"


def _archive(evt: Path, consumed_dir: Path, event_id: str) -> None:
    """Move a handled (acked, or given-up-on) event out of the live queue so
    `_next_event` never sees it again, and clear its retry counter."""
    _attempts_path(consumed_dir, event_id).unlink(missing_ok=True)
    os.replace(evt, Path(consumed_dir) / f"{event_id}.json")


def watch_loop(kind: str, sid: str, state_dir, events_dir, consumed_dir, *,
                out=None, max_emits: int = DEFAULT_MAX_EMITS,
                ack_timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
                poll_seconds: float = DEFAULT_POLL_SECONDS,
                sleep=time.sleep) -> int:
    """The watch loop itself, decoupled from argument parsing and the
    daemon health check so tests can drive it directly against a tmp_path
    with a tiny `poll_seconds`/`ack_timeout_seconds` instead of the real
    30-minute ack window.
    """
    out = out if out is not None else sys.stdout
    state_dir = Path(state_dir)
    events_dir = Path(events_dir)
    consumed_dir = Path(consumed_dir)
    # Only if the workspace is still there. Both of these live INSIDE
    # state_dir, so creating them unconditionally recreates a state_dir that
    # retention or the stray sweep just removed -- and the reap check at the
    # top of the loop would then never fire.
    if state_dir.is_dir():
        events_dir.mkdir(parents=True, exist_ok=True)
        consumed_dir.mkdir(parents=True, exist_ok=True)

    ack_iterations = max(1, int(round(ack_timeout_seconds / poll_seconds)))

    while True:
        if not state_dir.is_dir():
            print(f"WEBCOMPANION_CANCELLED skill={kind} sid={sid}", file=out)
            return 0

        terminal = terminal_state(state_dir)
        if terminal is not None:
            break

        beat(state_dir)
        evt = _next_event(events_dir)
        if evt is None:
            sleep(poll_seconds)
            continue

        event_id = evt.stem
        if _ack_path(consumed_dir, event_id).exists():
            # Already acked -- e.g. a re-emitted event that was answered
            # right at the edge of a previous attempt.
            _archive(evt, consumed_dir, event_id)
            continue

        print(f"WEBCOMPANION_EVENT skill={kind} sid={sid} event_id={event_id}",
              file=out)
        print("---payload---", file=out)
        print(evt.read_text(), file=out)
        print("---end---", file=out)
        if hasattr(out, "flush"):
            out.flush()

        acked = False
        gave_up_to_terminal = False
        for _ in range(ack_iterations):
            if _ack_path(consumed_dir, event_id).exists():
                acked = True
                break
            if terminal_state(state_dir) is not None:
                gave_up_to_terminal = True
                break
            # Keep beating while blocked on the ack, or watcher_seen_at
            # goes stale and a live session looks dead.
            beat(state_dir)
            sleep(poll_seconds)

        if acked:
            _archive(evt, consumed_dir, event_id)
            continue
        if gave_up_to_terminal:
            continue  # the loop head reports the terminal banner and exits

        n = 0
        attempts_path = _attempts_path(consumed_dir, event_id)
        try:
            n = int(attempts_path.read_text().strip())
        except (OSError, ValueError):
            n = 0
        n += 1
        if n >= max_emits:
            _archive(evt, consumed_dir, event_id)
            # Giving up must be loud: one final banner so the user is told
            # their question was dropped, instead of a spinner that
            # silently vanishes.
            print(f"WEBCOMPANION_DROPPED skill={kind} sid={sid} event_id={event_id}",
                  file=out)
        else:
            atomic_write(attempts_path, str(n))

    if terminal == "cancelled":
        print(f"WEBCOMPANION_CANCELLED skill={kind} sid={sid}", file=out)
    else:
        print(f"WEBCOMPANION_FINISHED skill={kind} sid={sid}", file=out)
    return 0


class UnknownSession(Exception):
    """No registry row matches this kind and sid (or slug)."""


def resolve_session_dirs(kind: str, sid: str) -> dict:
    """The directories the daemon actually created for this session.

    Reads the daemon's own registry file rather than recomputing a path from
    `Config` -- recomputing cannot tell an existing session from a typo, and
    the function that recomputes (`paths.make_session_dirs`) creates what it
    is asked about. Raises `UnknownSession` when nothing matches.
    """
    registry = Registry(paths.state_root())
    registry.rehydrate()
    resolved = registry.resolve(sid, kind=kind)
    if resolved is None:
        raise UnknownSession(sid)
    dirs = registry.lookup(resolved)
    if dirs is None:
        raise UnknownSession(sid)
    return dirs


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion watch")
    p.add_argument("--kind", required=True)
    p.add_argument("--sid", required=True)
    p.add_argument("--max-emits", type=int, default=DEFAULT_MAX_EMITS)
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        dirs = resolve_session_dirs(args.kind, args.sid)
    except UnknownSession:
        print(f"webcompanion watch: no such session: kind={args.kind} "
              f"sid={args.sid}\n"
              f"  the daemon has no registered session by that id or slug; "
              f"watch never creates one.\n"
              f"  list what exists:  webcompanion status", file=sys.stderr)
        return 1
    return watch_loop(args.kind, args.sid, dirs["state_dir"],
                       dirs["events_dir"], dirs["consumed_dir"],
                       max_emits=args.max_emits)
