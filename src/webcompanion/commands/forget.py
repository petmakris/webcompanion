"""`webcompanion forget` -- delete a session and everything in it.

The one thing a user could not do. `end` and `cancel` write a marker and change
nothing else, so a machine accumulated every session it had ever served: the
listing grew without bound, and dropping a single row meant stopping the daemon
and editing the registry by hand.

IRREVERSIBLE, and there is no second copy. What goes is the registry row and the
whole workspace directory -- items, comment threads, uploaded images, the event
queue. A session still LIVE is refused unless `--force`: the one deletion nobody
means to make is of something still running, and naming a finished session is
intent enough on its own.

`--sid` takes a slug as readily as a session id, which is what makes this usable
by hand; a slug that exists under more than one kind is a 409 naming them, so
add `--kind` to say which.
"""
from __future__ import annotations

import argparse
import json
import sys

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion forget")
    p.add_argument("--sid", required=True, help="session id or slug")
    p.add_argument("--kind", default="",
                   help="disambiguate a slug that exists under more than one kind")
    p.add_argument("--force", action="store_true",
                   help="delete it even though it is still live")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        res = client.forget(args.sid, force=args.force, kind=args.kind)
    except HttpError as e:
        if e.status == 409:
            print(f"webcompanion: {e}", file=sys.stderr)
            return 2
        return report(e)
    except (DaemonUnreachable, ContractMismatch) as e:
        return report(e)
    print(json.dumps(res))
    return 0
