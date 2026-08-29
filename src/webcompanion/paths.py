"""Where a session's files live on disk.

One daemon now holds sessions that used to live under five separate roots.
Merging them naively would let two kinds collide: `register_with_slug` dedups
slugs globally, so /annotate and /deck could no longer both own `my-plan` —
one would silently become `my-plan-2`, changing a URL a user has memorised.
It would also widen garbage collection: the stray sweep removes any
sid-shaped directory no registry row points at, so a registry bug in one kind
could delete another kind's workspaces.

Namespacing by kind fixes both. A workspace is
`<workspace_root>/<kind>/<sid>/`, slugs are unique within a kind, and the
sweep for one kind can only ever reach that kind's directory.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from webcompanion.atomic import write_text_atomic
from webcompanion.config import Config

# A kind names a directory, so it must not be able to escape one.
VALID_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
VALID_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

MARKER_FILE = "workspace.json"

_SUBDIRS = {
    "state_dir": ("state",),
    "items_dir": ("items",),
    "threads_dir": ("threads",),
    "events_dir": ("state", "events"),
    "consumed_dir": ("state", "consumed"),
    "assets_dir": ("assets",),
}


def state_root() -> Path:
    """Where the registry and config live. Not overridable — the CLI and the
    IntelliJ plugin both hardcode this path to find the config file, so an
    override would split the two halves of one directory."""
    return Path("~/.claude/webcompanion").expanduser()


def workspace_root(cfg: Config) -> Path:
    if cfg.workspace_root is not None:
        return cfg.workspace_root
    return state_root() / "workspaces"


def kind_root(cfg: Config, kind: str) -> Path:
    if not VALID_KIND_RE.match(kind or ""):
        raise ValueError(f"invalid kind: {kind!r}")
    return workspace_root(cfg) / kind


def make_session_dirs(cfg: Config, kind: str, sid: str) -> dict[str, Path]:
    if not VALID_SID_RE.match(sid or ""):
        raise ValueError(f"invalid sid: {sid!r}")
    base = kind_root(cfg, kind) / sid
    dirs = {key: base.joinpath(*rel) for key, rel in _SUBDIRS.items()}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def base_of(dirs: dict) -> Path:
    """The workspace's top directory, from a `dirs` mapping."""
    return Path(dirs["items_dir"]).parent


def write_marker(base: Path, sid: str, kind: str, cwd: str) -> None:
    """Record which project this workspace belongs to, inside the workspace.

    Best-effort: a workspace that fails to describe itself is still usable,
    and refusing to create one over a marker write would be worse than a
    missing marker.
    """
    try:
        write_text_atomic(
            Path(base) / MARKER_FILE,
            json.dumps({"sid": sid, "kind": kind, "cwd": str(cwd)}, indent=2),
        )
    except OSError:
        pass


def read_marker(base: Path) -> dict:
    try:
        data = json.loads((Path(base) / MARKER_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
