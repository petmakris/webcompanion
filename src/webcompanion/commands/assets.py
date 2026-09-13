"""`webcompanion assets` -- register a session's renderer directory.

The daemon serves a session's page from exactly one directory on local disk,
named by the client that owns the session. Until this command existed, saying
which directory was the one thing a client could NOT do through the CLI: it had
to POST /s/{sid}/api/assets itself, which meant building a request body,
learning a route, and sending the contract header -- the three things
`push.py` says the CLI exists to spare a caller.

Clients inside this project's own tree got away with it by importing a shared
Python client. A client in another repository has no such import, so without
this verb the CLI is only ALMOST the whole seam, and "almost" is what pushes
the next client into speaking HTTP.

Idempotent by design, and meant to be re-sent on every push: a plugin that has
moved on disk since its session was created still resolves.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion assets")
    p.add_argument("--sid", required=True)
    p.add_argument("--static-root", required=True,
                   help="directory holding the session's renderer")
    p.add_argument("--entry", default="",
                   help="module inside static-root to load first (e.g. entry.js)")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    # Checked here as well as in the daemon. The daemon's 400 is correct but
    # arrives as an HTTP error about a path the caller typed, and a client
    # that mistyped its own renderer directory should be told so in those words.
    root = Path(args.static_root)
    if not root.is_dir():
        print(f"webcompanion: {args.static_root} is not a directory", file=sys.stderr)
        return 1

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        client.register_assets(args.sid, str(root.resolve()), args.entry or None)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)
    return 0
