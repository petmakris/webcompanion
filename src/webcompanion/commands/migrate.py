"""`webcompanion migrate` -- move workspaces out of the five per-skill roots
this package replaces, into the single workspace root under one kind.

THIS MOVES DATA THAT CANNOT BE RECREATED. Retention in the old system
defaults to infinite and `resume <slug>` is a shipped, documented feature, so
a user can have workspaces going back to install day and no backup. The bias
throughout is: never lose a workspace, even one that can't be fully
converted -- move it anyway and flag it, rather than skip it or discard it.

HAZARD FOR ANYONE TESTING THIS MODULE AGAINST REAL DATA: copying an old
root's directory tree to a scratch location does NOT isolate a run.
`sessions.json` stores each session's directories as ABSOLUTE paths pointing
at wherever they originally lived; copying the JSON files to a new location
does not rewrite those paths. `plan()` pointed at a copied root still
resolves `old_base` to the ORIGINAL, live directory, and `apply()` would
still move it for real. This happened during Task 17 review: a copy of the
user's five real roots was migrated as an isolation test, and it moved all
30 real workspaces out of `~/.claude/` (recovered, byte-for-byte). The only
safe way to rehearse this module against real data is `apply(..., into=DIR)`
/ `webcompanion migrate --into DIR`, which copies each workspace instead of
moving it and leaves every source untouched -- never copy the directory
tree yourself and call `apply()` on it expecting isolation.

`plan()` reads each old root's `sessions.json` / `sessions_meta.json` and
returns rows describing what would move. It never writes, creates, or moves
anything -- a dry run is just printing this. `apply()` is the only function
that touches disk. Every row's `old_base` is the absolute path `plan()`
actually resolved from the registry (not the old root it was pointed at) --
inspect it before calling `apply()` if there is any doubt what will be
touched.

Two things changed shape between the two systems, and a migrated session
lands in one of three states as a result:

- Only `annotate` ever wrote `response/blocks.json` in the old layout; the
  other four skills (`deck`, `dataflow`, `walkthrough`, `interactive_review`)
  keep their content in their own state files (`steps.json`,
  `dataflow.json`, `diff.patch`, ...) that this migration does not know how
  to read. Their workspaces move intact, but their content is not converted
  into items -- meta gets `needs_repush: true`, and it is each skill's own
  job (plan 3) to re-push its content once it speaks the new contract. This
  is the ordinary outcome for every non-`annotate` session, not a problem.
- An `annotate` session's `blocks.json` is read once and written through
  `items.put_many`, keyed by each block's own `id` -- `migrated`.
- An `annotate` session whose `blocks.json` cannot be read at all (missing,
  corrupt JSON, wrong shape, or a batch `items.put_many` itself rejects) is
  marked `read_only: true` instead: moved, not discarded, but the daemon
  must not pretend it is editable when there is nothing to edit.

Comment threads are a separate channel from item content and move
regardless of the outcome above: the old layout wrote them under
`<base>/state/threads/`, the new one reads them from `<base>/threads/`
(`threads_dir`). Since the whole tree already lands at the new base by the
time this runs, relocating them is a same-filesystem move of files whose
names are already `encode_anchor` output -- nothing is re-encoded.

Old registry rows have no `kind` -- it comes from which of the five roots a
row was found under, which also happens to already be the new kind name.
Old slugs were deduped globally; the new registry dedupes per kind, so two
old roots that both used slug `my-plan` land side by side, unrenamed.

Idempotent: the old `sessions.json` is never rewritten by this module, so a
second run sees the same rows `plan()` saw the first time. `apply()` treats a
sid already known to the registry, or whose `old_base` no longer exists on
disk, as already done and moves nothing for it. Verified against a copy of
30 real sessions across all five roots: a second `--apply` moved 0.
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

# Only this kind ever wrote response/blocks.json in the old layout. Every
# session's `dirs` carries a `response_dir` key regardless of kind (it is
# part of the old shared per-session layout every skill got for free), so
# a missing/empty blocks.json under a non-annotate kind is not corruption --
# it is simply a kind that was never going to have one. Branching on kind
# here, not on whether the file happens to exist, is what keeps the two
# apart (see the module docstring).
_ITEM_FORMAT_KIND = "annotate"

# Where the old layout wrote per-anchor comment threads, relative to a
# session's base directory. The new layout's threads_dir is `<base>/threads`
# (see paths._SUBDIRS) -- a different directory, not a renamed one.
_OLD_THREADS_REL = ("state", "threads")


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


def _relocate_threads(new_base: Path, threads_dir: Path) -> int:
    """Move every thread file from the old `<base>/state/threads/` (already
    sitting at `new_base` after the whole-tree move) into the new
    `threads_dir`. Filenames are kept as-is -- they are already
    `encode_anchor` output, so this is a move, not a re-encode. Returns the
    count moved, so a caller can verify nothing was left behind.
    """
    old_threads = Path(new_base).joinpath(*_OLD_THREADS_REL)
    if not old_threads.is_dir():
        return 0
    threads_dir = Path(threads_dir)
    threads_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for f in sorted(old_threads.iterdir()):
        if not f.is_file():
            continue
        dest = threads_dir / f.name
        if dest.exists():
            continue  # never guess which of two files claiming one name is right
        try:
            shutil.move(str(f), str(dest))
            moved += 1
        except (OSError, shutil.Error):
            continue
    return moved


def apply(plan_rows: list[dict], cfg: cfgmod.Config, registry: Registry,
          into: Path | None = None) -> dict[str, int]:
    """Perform the move `plan()` described: relocate each workspace under its
    kind, relocate its comment threads, convert an `annotate` session's
    `blocks.json` into items, and register it. See the module docstring for
    the three content outcomes (`migrated`, `needs_repush`, `read_only`).

    `into`, if given, turns this into a rehearsal: every workspace is
    COPIED into `<into>/<kind>/<sid>` instead of moved, `cfg` is ignored for
    the purpose of choosing a destination (the destination is `into`,
    unconditionally), and every source directory is left exactly as `plan()`
    found it. Same plan, same conversion, same categorisation, same
    returned summary shape -- copy versus move is the only difference. Pass
    a throwaway `registry` for a rehearsal; this function still calls
    `registry.persist()` on it.

    Idempotent two ways: a sid `registry` already knows about (same process,
    same run, or rehydrated from a prior run) is skipped, and a sid whose
    `old_base` no longer exists on disk (an earlier `--apply` already moved
    it) is skipped too -- the old `sessions.json` is never rewritten by this
    module, so that disk check is what makes re-running the CLI safe across
    process restarts. (A rehearsal never removes `old_base`, so re-running a
    rehearsal into the same `into` directory hits the destination-collision
    check below instead -- rehearsals are meant to be disposable, not
    idempotent across repeated runs into one target.)
    """
    dest_cfg = cfgmod.Config(workspace_root=Path(into)) if into is not None else cfg
    summary = {
        "moved": 0, "migrated": 0, "needs_repush": 0, "read_only": 0,
        "already_done": 0, "errors": 0,
    }
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
            new_base = paths.kind_root(dest_cfg, kind) / sid
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
            if into is not None:
                shutil.copytree(str(old_base), str(new_base))
            else:
                shutil.move(str(old_base), str(new_base))
        except (OSError, shutil.Error):
            summary["errors"] += 1
            continue

        dirs = paths.make_session_dirs(dest_cfg, kind, sid)
        _relocate_threads(new_base, dirs["threads_dir"])

        if kind == _ITEM_FORMAT_KIND:
            moved_content_path = None
            content_path = row.get("content_path")
            if content_path is not None:
                try:
                    moved_content_path = new_base / Path(content_path).relative_to(old_base)
                except ValueError:
                    moved_content_path = Path(content_path)

            bodies = _load_blocks(moved_content_path)
            if bodies is None:
                outcome = "read_only"
            else:
                outcome = "migrated"
                if bodies:
                    try:
                        items.put_many(dirs["items_dir"], bodies)
                    except ValueError:
                        outcome = "read_only"
        else:
            # Not the kind that ever spoke blocks.json -- its content is
            # intact on disk under its own old format, just not converted.
            # Its own skill re-pushes it once it speaks the new contract.
            outcome = "needs_repush"

        meta_base = {"title": row.get("title", row["slug"])}
        if outcome == "read_only":
            meta_base["read_only"] = True
        elif outcome == "needs_repush":
            meta_base["needs_repush"] = True

        registry.create(kind, sid, dirs, meta_base, row.get("cwd", ""),
                         explicit_slug=row["slug"])
        paths.write_marker(paths.base_of(dirs), sid, kind, row.get("cwd", ""))
        registry.persist()

        summary["moved"] += 1
        summary[outcome] += 1
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="webcompanion migrate")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                       help="print what would move; the default")
    mode.add_argument("--apply", action="store_true",
                       help="perform the migration")
    mode.add_argument("--into", metavar="DIR",
                       help="rehearse safely: copy into DIR instead of moving, "
                            "leaving every source untouched")
    return p


def _print_summary(prefix: str, summary: dict) -> None:
    print(f"{prefix}moved: {summary['moved']}  "
          f"migrated: {summary['migrated']}  "
          f"needs_repush: {summary['needs_repush']}  "
          f"read_only: {summary['read_only']}  "
          f"already_done: {summary['already_done']}  "
          f"errors: {summary['errors']}")


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    rows = plan(_default_old_roots())

    if args.into:
        if not rows:
            print("webcompanion migrate: nothing to migrate.")
            return 0
        into_dir = Path(args.into)
        # Said on every rehearsal run, unmissable, so a rehearsal is never
        # mistaken for a completed migration or vice versa -- see the
        # module docstring for why a directory copy alone does not isolate
        # this from the real, live workspaces.
        print(f"rehearsal: sources left in place -- copying into {into_dir}")
        dest_cfg = cfgmod.Config(workspace_root=into_dir)
        registry = Registry(into_dir / ".registry")
        summary = apply(rows, dest_cfg, registry, into=into_dir)
        _print_summary("rehearsal: sources left in place  --  ", summary)
        return 1 if summary["errors"] else 0

    if not args.apply:
        if not rows:
            print("webcompanion migrate: nothing to migrate.")
            return 0
        cfg = cfgmod.load()  # for display only -- plan() itself never loads one
        for row in rows:
            note = ("will migrate its content" if row["kind"] == _ITEM_FORMAT_KIND
                    else "content stays as-is; its own skill re-pushes it later")
            dest = paths.kind_root(cfg, row["kind"]) / row["sid"]
            print(f"{row['kind']}/{row['sid']} ({row['slug']}) -- {note}\n"
                  f"    {row['old_base']} -> {dest}")
        print(f"\n{len(rows)} session(s) found. Re-run with --apply to migrate them, "
              f"or --into DIR to rehearse first.")
        return 0

    cfg = cfgmod.load()
    registry = Registry(paths.state_root())
    registry.rehydrate()
    summary = apply(rows, cfg, registry)
    _print_summary("", summary)
    return 1 if summary["errors"] else 0
