"""Server-derived per-anchor version chains.

An item's version is never something a client writes. It is computed by
hashing the item's canonical JSON body and comparing against a per-anchor
hash chain in a sidecar file; the reported version is the chain's length, a
value that can only grow when content actually changes.

This kills two failure modes at once: a client rewriting unrelated items
bumps their versions for no reason, and byte-churn that changes nothing
visible bumps versions anyway. The daemon cannot filter cosmetic noise the
way annotate's version of this module did — it does not know that a body
contains markdown, or HTML, or a diagram spec — so normalisation belongs in
whichever client understands the format. Canonical JSON is the part that is
genuinely generic: key order is not a content change.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from webcompanion.atomic import write_text_atomic


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def body_hash(body: dict) -> str:
    h = hashlib.sha1()
    h.update(_canonical_json(body).encode("utf-8"))
    return h.hexdigest()


def _load_chain(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: list(v) for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
    }


def derive_versions(chain_path: Path, bodies: dict[str, dict]) -> dict[str, int]:
    """Return {anchor: version} for `bodies`, growing the chain where content
    changed and pruning anchors no longer present.

    Concurrent calls converge: both read the same tail, both append the same
    hash, and last-writer-wins leaves identical state.
    """
    chain_path = Path(chain_path)
    chain = _load_chain(chain_path)
    changed = False

    for stale in [k for k in chain if k not in bodies]:
        del chain[stale]
        changed = True

    for anchor, body in bodies.items():
        if not isinstance(anchor, str):
            continue
        h = body_hash(body if isinstance(body, dict) else {"_": body})
        history = chain.setdefault(anchor, [])
        if not history or history[-1] != h:
            history.append(h)
            changed = True

    if changed:
        write_text_atomic(chain_path, json.dumps(chain, indent=2))

    return {a: len(chain.get(a, [])) or 1 for a in bodies if isinstance(a, str)}
