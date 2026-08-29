"""Session identity: sid, slug, kind, and the per-session change counter.

The registry knows what sessions exist and where their directories are. It
knows nothing about their content — that is items.py's job — and nothing
about HTTP.

Two things differ from the per-skill registry this replaces. Slugs are unique
within a kind rather than globally, because one daemon must not make
/annotate and /deck fight over `my-plan`. And change notification is a
monotonic counter rather than a threading.Event that is set and immediately
cleared: the old shape had a real lost-wakeup window between set() and
clear(), survivable only because the SSE loop re-read on a 30s timeout.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

from webcompanion.atomic import write_text_atomic
from webcompanion.paths import VALID_SID_RE


class Registry:
    def __init__(self, state_root: Path):
        self._state_root = Path(state_root)
        self._sessions: dict[str, dict] = {}
        self._meta: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._counters: dict[str, int] = {}

    # ── files ────────────────────────────────────────────────────────────
    @property
    def sessions_file(self) -> Path:
        return self._state_root / "sessions.json"

    @property
    def sessions_meta_file(self) -> Path:
        return self._state_root / "sessions_meta.json"

    # ── identity ─────────────────────────────────────────────────────────
    def make_sid(self) -> str:
        return f"{time.strftime('%y%m%d-%H%M%S')}-{secrets.token_hex(8)}"

    @staticmethod
    def _slugify(text: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
        return s[:40].strip("-")

    def create(self, kind: str, sid: str, dirs: dict, meta_base: dict,
               cwd: str, explicit_slug: str = "") -> str:
        """Pick a free slug within `kind` and register dirs + meta atomically.

        Pick-and-insert happens inside one lock acquisition. Splitting them
        is a check-then-act race: two concurrent creates with the same title
        both compute the same slug before either registers it.
        """
        base = (
            self._slugify(explicit_slug)
            or self._slugify(meta_base.get("title", ""))
            or self._slugify(Path(cwd).name)
            or "session"
        )
        with self._lock:
            taken = {
                m.get("slug") for m in self._meta.values()
                if m.get("kind") == kind and m.get("slug")
            }
            slug = base
            if slug in taken:
                n = 2
                while f"{base}-{n}" in taken:
                    n += 1
                slug = f"{base}-{n}"
            self._sessions[sid] = {**dirs, "_sid": sid, "_cwd": str(cwd), "_kind": kind}
            self._meta[sid] = {**meta_base, "slug": slug, "kind": kind,
                               "cwd": str(cwd), "created_at": int(time.time())}
        return slug

    def lookup(self, sid: str) -> dict | None:
        with self._lock:
            return self._sessions.get(sid)

    def resolve(self, key: str, kind: str | None = None) -> str | None:
        """A sid, or a slug within `kind`. A slug without a kind is ambiguous
        once two kinds can hold the same one, so it resolves only if exactly
        one session matches."""
        with self._lock:
            if key in self._sessions:
                return key
            matches = [
                sid for sid, m in self._meta.items()
                if m.get("slug") == key and sid in self._sessions
                and (kind is None or m.get("kind") == kind)
            ]
        return matches[0] if len(matches) == 1 else None

    def get_meta(self, sid: str) -> dict:
        with self._lock:
            return dict(self._meta.get(sid, {}))

    def unregister(self, sid: str) -> None:
        """Drop a dead session in memory, freeing its slug and its counter.

        Does NOT persist — persist() takes the lock itself, so calling it here
        would nest; the caller's next persist() snapshots the removal.
        """
        with self._lock:
            self._sessions.pop(sid, None)
            self._meta.pop(sid, None)
            self._counters.pop(sid, None)

    def items(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self._sessions.items())

    def find(self, cwd: str | None = None, kind: str | None = None) -> list[tuple[str, dict]]:
        """Sessions matching cwd and/or kind.

        The kind filter is why this exists. Both IntelliJ clients discover by
        cwd alone, which was unambiguous only while they hit different ports.
        """
        out = []
        for sid, dirs in self.items():
            if cwd is not None and str(dirs.get("_cwd", "")) != str(cwd):
                continue
            if kind is not None and str(dirs.get("_kind", "")) != kind:
                continue
            out.append((sid, dirs))
        return out

    def list_all(self) -> list[tuple[str, dict]]:
        out = self.items()
        out.sort(key=lambda kv: kv[0], reverse=True)
        return out

    # ── persistence ──────────────────────────────────────────────────────
    def persist(self) -> None:
        self._state_root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            snapshot = {
                sid: {k: str(v) for k, v in dirs.items()}
                for sid, dirs in self._sessions.items()
            }
            meta_snapshot = {sid: dict(m) for sid, m in self._meta.items()}
        write_text_atomic(self.sessions_file, json.dumps(snapshot, indent=2))
        write_text_atomic(self.sessions_meta_file, json.dumps(meta_snapshot, indent=2))

    def rehydrate(self) -> None:
        """Restore rows whose directories still exist. A row pointing at a
        deleted tree is dropped, not resurrected.

        Reads absolute paths straight out of sessions.json, so no Config is
        needed here.
        """
        try:
            snapshot = json.loads(self.sessions_file.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(snapshot, dict):
            return
        restored: dict[str, dict] = {}
        for sid, dirs in snapshot.items():
            if not VALID_SID_RE.match(sid) or not isinstance(dirs, dict):
                continue
            paths_map = {k: (v if k.startswith("_") else Path(v)) for k, v in dirs.items()}
            real = [v for k, v in paths_map.items() if not k.startswith("_")]
            if not real or not all(isinstance(p, Path) and p.is_dir() for p in real):
                continue
            restored[sid] = paths_map
        with self._lock:
            self._sessions.update(restored)
        try:
            msnap = json.loads(self.sessions_meta_file.read_text())
        except (OSError, json.JSONDecodeError):
            msnap = {}
        if isinstance(msnap, dict):
            with self._lock:
                live = set(self._sessions)
                self._meta.update({
                    sid: m for sid, m in msnap.items()
                    if sid in live and isinstance(m, dict)
                })

    # ── change notification ──────────────────────────────────────────────
    def version(self, sid: str) -> int:
        with self._lock:
            return self._counters.get(sid, 0)

    def note_change(self, sid: str) -> int:
        """Bump this session's counter and wake every waiter. Returns the new
        value."""
        with self._cond:
            v = self._counters.get(sid, 0) + 1
            self._counters[sid] = v
            self._cond.notify_all()
            return v

    def wait_for_change(self, sid: str, since: int, timeout: float) -> int:
        """Block until this session's counter exceeds `since`, or `timeout`.

        Returns the current counter either way. A change that lands between
        the caller reading its snapshot and calling this is NOT lost: the
        comparison is against a value, not an edge.
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._counters.get(sid, 0) <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return self._counters.get(sid, 0)
