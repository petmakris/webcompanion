from __future__ import annotations

import argparse
import sys

from webcompanion import CONTRACT, __version__

SUBCOMMANDS = (
    "serve", "push", "update", "assets", "reply", "end", "unfinish", "watch", "ack",
    "install-service", "uninstall", "status", "doctor", "migrate",
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion", add_help=True)
    p.add_argument("--version", action="store_true",
                   help="print the package version and contract, then exit")
    p.add_argument("command", nargs="?", choices=SUBCOMMANDS)
    p.add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse exits 2 on an invalid choice; keep that as our return code
        # rather than letting it kill an embedding process.
        return 2
    if args.version or not args.command:
        print(f"webcompanion {__version__} (contract {CONTRACT})")
        return 0
    return _dispatch(args.command, args.rest)


def _dispatch(command: str, rest: list[str]) -> int:
    # Subcommand modules are imported lazily so `--version` and `--help` stay
    # fast and so a broken optional command cannot break the whole CLI.
    module_name = command.replace("-", "_")
    from importlib import import_module
    mod = import_module(f"webcompanion.commands.{module_name}")
    return int(mod.run(rest))
