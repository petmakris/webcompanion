"""`webcompanion push` -- create a session and load its initial items.

The CLI is the only seam clients use: a caller hands over a kind, a cwd, and
a JSON file of items, and never builds a request body or learns a route.

The 5MB body limit used to live inside `POST /api/sessions` on the server
that `interactive_review` called with a `gh` diff. That route lost the
check when the five per-skill servers merged into this daemon -- nothing
server-side rejects an oversized push any more -- so it is enforced here,
before the file is even sent, or it evaporates entirely.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from webcompanion.client import ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands._common import client_from_config, preflight, report

MAX_PUSH_BYTES = 5 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion push")
    p.add_argument("--kind", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--items", required=True,
                   help="path to a JSON file shaped {\"items\": {anchor: body}}")
    p.add_argument("--supersede", action="store_true",
                   help="end this kind+cwd's other live sessions")
    p.add_argument("--eval", action="store_true", dest="eval_",
                   help="print WC_SID=/WC_URL=/WC_SLUG= for `eval`")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    doc_path = Path(args.items)
    try:
        raw = doc_path.read_bytes()
    except OSError as e:
        print(f"webcompanion: could not read {args.items}: {e}", file=sys.stderr)
        return 1

    # Enforced BEFORE parsing or sending -- a caller with a 40MB diff should
    # not pay for a JSON parse we are about to refuse anyway.
    if len(raw) > MAX_PUSH_BYTES:
        print(f"webcompanion: {args.items} is too large "
              f"({len(raw)} bytes, limit {MAX_PUSH_BYTES})", file=sys.stderr)
        return 1

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"webcompanion: {args.items} is not valid JSON: {e}", file=sys.stderr)
        return 1
    bodies = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(bodies, dict):
        print(f"webcompanion: {args.items} must contain an \"items\" object",
              file=sys.stderr)
        return 1

    client = client_from_config()
    rc = preflight(client)
    if rc is not None:
        return rc

    try:
        created = client.create(args.kind, args.cwd, title=args.title,
                                 slug=args.slug, supersede=args.supersede)
        if bodies:
            client.put_items(created["sid"], bodies)
    except (DaemonUnreachable, ContractMismatch, HttpError) as e:
        return report(e)

    if args.eval_:
        # shlex.quote leaves a value made only of safe characters (a plain
        # sid, a loopback URL) unquoted, and single-quotes anything else --
        # so a slug containing a shell metacharacter cannot inject when the
        # caller does `eval "$(webcompanion push --eval ...)"`.
        print(f"WC_SID={shlex.quote(created['sid'])}")
        print(f"WC_URL={shlex.quote(created['url'])}")
        print(f"WC_SLUG={shlex.quote(created['slug'])}")
    else:
        print(created["url"])
    return 0
