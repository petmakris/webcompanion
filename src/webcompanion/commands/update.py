"""`webcompanion update` -- replace one item's body by anchor."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion update")
    p.add_argument("--sid", required=True)
    p.add_argument("--anchor", required=True)
    p.add_argument("--body", required=True,
                   help="path to a JSON file with the item's new body")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    try:
        body = json.loads(Path(args.body).read_text())
    except OSError as e:
        print(f"webcompanion: could not read {args.body}: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"webcompanion: {args.body} is not valid JSON: {e}", file=sys.stderr)
        return 1

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        client.put_item(args.sid, args.anchor, body)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)
    return 0
