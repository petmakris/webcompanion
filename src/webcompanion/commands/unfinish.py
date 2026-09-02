"""`webcompanion unfinish` -- reopen a finished or cancelled session."""
from __future__ import annotations

import argparse

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion unfinish")
    p.add_argument("--sid", required=True)
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        client.unfinish(args.sid)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)
    return 0
