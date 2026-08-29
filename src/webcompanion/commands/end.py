"""`webcompanion end` -- finish or cancel a session."""
from __future__ import annotations

import argparse

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion end")
    p.add_argument("--sid", required=True)
    p.add_argument("--cancel", action="store_true",
                   help="cancel the session instead of finishing it")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        if args.cancel:
            client.cancel(args.sid)
        else:
            client.finish(args.sid)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)
    return 0
