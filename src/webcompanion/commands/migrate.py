"""`webcompanion migrate` -- move workspaces out of the five per-skill roots
this package replaces, into the single workspace root under one kind.

THIS MOVES DATA THAT CANNOT BE RECREATED. Retention in the old system
defaults to infinite and `resume <slug>` is a shipped, documented feature, so
a user can have workspaces going back to install day and no backup. The bias
throughout is: never lose a workspace, even one that can't be fully
converted -- move it anyway and flag it, rather than skip it or discard it.

`plan()` reads each old root's `sessions.json` / `sessions_meta.json` and
returns rows describing what would move. It never writes, creates, or moves
anything -- a dry run is just printing this. `apply()` is the only function
that touches disk.

The content channel changed between the two systems: the old workspace kept
blocks in `response/blocks.json`; the new store is one item per anchor under
`items_dir`. So each session's `blocks.json` is read once, per block, and
written through `items.put_many`, keyed by the block's own `id`. Where that
read fails -- a missing file, corrupt JSON, a batch `items.put_many` rejects
-- the session is still moved, just marked `read_only: true` in its meta, so
the daemon won't pretend it is editable and the user still has the data.

Old registry rows have no `kind` -- it comes from which of the five roots a
row was found under, which also happens to already be the new kind name
(`annotate`, `deck`, `dataflow`, `walkthrough`, `interactive_review`). Old
slugs were deduped globally; the new registry dedupes per kind, so two old
roots that both used slug `my-plan` land side by side, unrenamed.

Idempotent: the old `sessions.json` is never rewritten by this module, so a
second run sees the same rows `plan()` saw the first time. `apply()` treats a
sid already known to the registry, or whose `old_base` no longer exists on
disk, as already done and moves nothing for it.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from webcompanion import config as cfgmod
from webcompanion import items, paths
from webcompanion.registry import Registry

# The five per-skill state roots this package replaces. Each root's own
# directory name already matches its `kind` name in the new registry
# (`~/.claude/annotate`, `~/.claude/deck`, ...), so no renaming is needed.
_OLD_SKILLS = ("annotate", "deck", "dataflow", "walkthrough", "interactive_review")


def _default_old_roots() -> list[Path]:
    return [Path.home() / ".claude" / name for name in _OLD_SKILLS]


def _read_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def plan(old_roots: list[Path]) -> list[dict]:
    """One row per session found under any of `old_roots`. Read-only: opens
    two known JSON files per root and does nothing else with the filesystem
    -- no `Config`, no `mkdir`, nothing under any other path.

    `plan()` takes no `Config`, so it cannot know the real destination
    `apply()` will use -- that depends on whichever `cfg` is handed to
    `apply()` later, possibly a different one (a service's config differs
    from a test's, for instance). `new_base` here is therefore a relative,
    informational `<kind>/<sid>`, not an absolute path; `run()` resolves the
    real absolute destination for its dry-run print using the one `cfg` it
    actually loads, and `apply()` resolves its own from the `cfg` it is
    given -- neither trusts this field.
    """
    rows: list[dict] = []
    for old_root in old_roots:
        old_root = Path(old_root)
        kind = old_root.name
        if not paths.VALID_KIND_RE.match(kind):
            continue  # not a directory name that could be a kind -- not ours
        sessions = _read_json(old_root / "sessions.json", {})
        if not isinstance(sessions, dict):
            continue
        meta = _read_json(old_root / "sessions_meta.json", {})
        if not isinstance(meta, dict):
            meta = {}

        for sid, dirs in sessions.items():
            if not isinstance(dirs, dict):
                continue
            state_dir = dirs.get("state_dir")
            response_dir = dirs.get("response_dir")
            base_ref = state_dir or response_dir
            if not base_ref:
                continue  # nothing recognizable to locate this session's tree
            old_base = Path(base_ref).parent
            meta_row = meta.get(sid) if isinstance(meta.get(sid), dict) else {}
            slug = meta_row.get("slug") or sid
            content_path = Path(response_dir) / "blocks.json" if response_dir else None
            rows.append({
                "sid": sid,
                "kind": kind,
                "slug": slug,
                "title": meta_row.get("title", slug),
                "cwd": dirs.get("_cwd", ""),
                "old_base": old_base,
                "new_base": Path(kind) / sid,
                "content_path": content_path,
            })
    return rows


def _load_blocks(content_path: Path | None) -> dict[str, dict] | None:
    """Old-format `blocks.json` (`{"blocks": [...]}`) into an anchor -> body
    mapping keyed by each block's own `id`. Returns None -- never raises --
    when the file is missing, unreadable, or shaped wrong, so the caller can
    fall back to marking the session read-only instead of losing it.
    """
    if content_path is None:
        return None
    raw = _read_json(Path(content_path), None)
    if not isinstance(raw, dict):
        return None
    blocks = raw.get("blocks")
    if not isinstance(blocks, list):
        return None
    bodies: dict[str, dict] = {}
    for block in blocks:
        if not isinstance(block, dict):
            return None
        bid = block.get("id")
        if not isinstance(bid, str) or not bid:
            return None
        bodies[bid] = block
    return bodies


def apply(plan_rows: list[dict], cfg: cfgmod.Config, registry: Registry) -> dict[str, int]:
    """Perform the move `plan()` described: relocate each workspace under its
    kind, convert its `blocks.json` into items, and register it.

    Idempotent two ways: a sid `registry` already knows about (same process,
    same run, or rehydrated from a prior run) is skipped, and a sid whose
    `old_base` no longer exists on disk (an earlier `--apply` already moved
    it) is skipped too -- the old `sessions.json` is never rewritten by this
    module, so that disk check is what makes re-running the CLI safe across
    process restarts.
    """
    summary = {"moved": 0, "already_done": 0, "read_only": 0, "errors": 0}
    for row in plan_rows:
        sid, kind = row["sid"], row["kind"]

        if registry.lookup(sid) is not None:
            summary["already_done"] += 1
            continue

        old_base = Path(row["old_base"])
        if not old_base.is_dir():
            summary["already_done"] += 1
            continue

        try:
            new_base = paths.kind_root(cfg, kind) / sid
        except ValueError:
            summary["errors"] += 1
            continue
        if new_base.exists():
            # Something already claims this destination -- refuse to
            # clobber it rather than guess which copy is the real one.
            summary["errors"] += 1
            continue

        try:
            new_base.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_base), str(new_base))
        except (OSError, shutil.Error):
            summary["errors"] += 1
            continue

        dirs = paths.make_session_dirs(cfg, kind, sid)

        moved_content_path = None
        content_path = row.get("content_path")
        if content_path is not None:
            try:
                moved_content_path = new_base / Path(content_path).relative_to(old_base)
            except ValueError:
                moved_content_path = Path(content_path)

        bodies = _load_blocks(moved_content_path)
        read_only = bodies is None
        if bodies:
            try:
                items.put_many(dirs["items_dir"], bodies)
            except ValueError:
                read_only = True

        meta_base = {"title": row.get("title", row["slug"])}
        if read_only:
            meta_base["read_only"] = True

        registry.create(kind, sid, dirs, meta_base, row.get("cwd", ""),
                         explicit_slug=row["slug"])
        paths.write_marker(paths.base_of(dirs), sid, kind, row.get("cwd", ""))
        registry.persist()

        summary["moved"] += 1
        if read_only:
            summary["read_only"] += 1
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion migrate")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                       help="print what would move; the default")
    mode.add_argument("--apply", action="store_true",
                       help="perform the migration")
    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    rows = plan(_default_old_roots())

    if not args.apply:
        if not rows:
            print("webcompanion migrate: nothing to migrate.")
            return 0
        cfg = cfgmod.load()  # for display only -- plan() itself never loads one
        for row in rows:
            flag = " " if row["content_path"] else " (no content found) "
            dest = paths.kind_root(cfg, row["kind"]) / row["sid"]
            print(f"{row['kind']}/{row['sid']} ({row['slug']}){flag}"
                  f"{row['old_base']} -> {dest}")
        print(f"\n{len(rows)} session(s) found. Re-run with --apply to migrate them.")
        return 0

    cfg = cfgmod.load()
    registry = Registry(paths.state_root())
    registry.rehydrate()
    summary = apply(rows, cfg, registry)
    print(f"moved: {summary['moved']}  read_only: {summary['read_only']}  "
          f"already_done: {summary['already_done']}  errors: {summary['errors']}")
    return 1 if summary["errors"] else 0
