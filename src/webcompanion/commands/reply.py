"""`webcompanion reply` -- append one message to an anchor's thread.

Reads the message text from a file, never from an argv string: a Claude
answer routinely contains backticks, `$(...)`, and quotes, and interpolating
that into a shell command is exactly the injection risk interactive_review's
SKILL.md already routes around by writing files first. This command is that
same pattern, generalized off the legacy per-skill reply_cli.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion reply")
    p.add_argument("--sid", required=True)
    p.add_argument("--anchor", required=True)
    p.add_argument("--text", required=True,
                   help="path to a file containing the reply's raw text")
    p.add_argument("--role", default="agent")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    try:
        text = Path(args.text).read_text()
    except OSError as e:
        print(f"webcompanion: could not read {args.text}: {e}", file=sys.stderr)
        return 1

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        client.append_thread(args.sid, args.anchor, text, role=args.role)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)
    return 0
