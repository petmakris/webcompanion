"""The item store — one JSON body per anchor, plus a derived version.

An item is opaque. The daemon stores the body verbatim, hashes it for
versioning, and hands it back. It never inspects the body's shape, because
the five clients that push items disagree completely about what one contains:
a markdown block, a slide, a diff hunk, a graph node, a walkthrough step.

Anchors are client-chosen and become filenames, so they are URL-quoted and
hashed past a length cap — the same encoding threads.py uses, so an item and
its comment thread land on matching on-disk names.

`derive_versions` takes an flock on a sidecar lock file spanning its whole
read-compute-write, and flock is per-process-and-fd: a nested call from
inside an already-running call would wait on a lock that cannot be released
until the outer call returns. So every public function here calls
`derive_versions` at most once, and never while another call to it is on the
stack — `snapshot` computes versions directly rather than going through
`versions_of` (which itself calls `derive_versions`).
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

from webcompanion.atomic import write_text_atomic
from webcompanion.versions import derive_versions

MAX_BODY_BYTES = 2 * 1024 * 1024
CHAIN_FILE = ".versions.json"
_MAX_NAME = 200


def valid_anchor(anchor: str) -> bool:
    if not isinstance(anchor, str) or not anchor:
        return False
    if "\x00" in anchor or "\n" in anchor:
        return False
    # Path traversal is defeated by quoting rather than by inspection, but an
    # anchor whose decoded form walks out of the directory is a client bug
    # worth rejecting loudly rather than silently storing under a mangled name.
    return ".." not in Path(anchor).parts


def encode_anchor(anchor: str) -> str:
    """URL-quote `anchor` into a filename stem, hashing past a length cap
    rather than truncating — truncation would collide two distinct long
    anchors that share a prefix onto the same file.

    Shared with the thread store (threads.py), so an item and its comment
    thread land on matching on-disk names.
    """
    enc = urllib.parse.quote(anchor, safe="")
    if len(enc) > _MAX_NAME:
        enc = "h_" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()
    return enc


def _path_for(items_dir: Path, anchor: str) -> Path:
    return Path(items_dir) / f"{encode_anchor(anchor)}.json"


def _validated_payload(anchor: str, body: dict) -> str:
    if not valid_anchor(anchor):
        raise ValueError(f"invalid anchor: {anchor!r}")
    payload = json.dumps({"anchor": anchor, "body": body})
    if len(payload.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError("item body too large")
    return payload


def put(items_dir: Path, anchor: str, body: dict) -> None:
    payload = _validated_payload(anchor, body)
    Path(items_dir).mkdir(parents=True, exist_ok=True)
    write_text_atomic(_path_for(items_dir, anchor), payload)


def put_many(items_dir: Path, bodies: dict, replace: bool = False) -> None:
    """Upsert every anchor in `bodies`. With replace=True, anchors absent from
    `bodies` are deleted — the shape a full document push wants.

    The whole batch is validated (anchor shape, body size) before any write
    happens, so a single bad anchor or oversized body among many raises
    ValueError with the store left completely unchanged — no partial write,
    and no replace-deletes run either. This is not a guarantee against an I/O
    failure partway through the write phase itself (disk full, permissions):
    that can still leave a partial batch on disk. Only the validation phase
    protects against the caller's own bad input, which is the failure mode
    that actually happens.
    """
    payloads = {anchor: _validated_payload(anchor, body) for anchor, body in bodies.items()}
    Path(items_dir).mkdir(parents=True, exist_ok=True)
    for anchor, payload in payloads.items():
        write_text_atomic(_path_for(items_dir, anchor), payload)
    if replace:
        for anchor in set(load_all(items_dir)) - set(bodies):
            delete(items_dir, anchor)


def delete(items_dir: Path, anchor: str) -> bool:
    try:
        _path_for(items_dir, anchor).unlink()
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False


def load_one(items_dir: Path, anchor: str) -> dict | None:
    try:
        raw = json.loads(_path_for(items_dir, anchor).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return raw.get("body") if isinstance(raw, dict) else None


def load_all(items_dir: Path) -> dict[str, dict]:
    items_dir = Path(items_dir)
    if not items_dir.is_dir():
        return {}
    out: dict[str, dict] = {}
    for p in sorted(items_dir.iterdir()):
        if p.suffix != ".json" or p.name == CHAIN_FILE:
            continue
        try:
            raw = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict) and isinstance(raw.get("anchor"), str):
            out[raw["anchor"]] = raw.get("body")
    return out


def versions_of(items_dir: Path) -> dict[str, int]:
    return derive_versions(Path(items_dir) / CHAIN_FILE, load_all(items_dir))


def snapshot(items_dir: Path) -> dict[str, dict]:
    # Calls derive_versions directly, exactly once — never through
    # versions_of — so this never nests the flock inside itself.
    bodies = load_all(items_dir)
    versions = derive_versions(Path(items_dir) / CHAIN_FILE, bodies)
    return {a: {"body": b, "version": versions.get(a, 1)} for a, b in bodies.items()}
