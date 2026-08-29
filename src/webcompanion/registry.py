"""Session identity: sid, slug, kind, and the per-session change counter.

The registry knows what sessions exist and where their directories are. It
knows nothing about their content — that is items.py's job — and nothing
about HTTP.

`sessions.json` is the one file whose loss deletes data: `cleanup.sweep_strays`
removes every sid-shaped directory no row points at, so a row that disappears
takes its workspace with it on the next daemon start. Two processes own that
file -- the daemon and `webcompanion migrate` -- so `persist()` locks it and
MERGES with what is on disk instead of overwriting from memory, and
`rehydrate()` reports whether it could be read at all rather than failing
silent. See the comments on both methods.

Two things differ from the per-skill registry this replaces. Slugs are unique
within a kind rather than globally, because one daemon must not make
/annotate and /deck fight over `my-plan`. And change notification is a
monotonic counter rather than a threading.Event that is set and immediately
cleared: the old shape had a real lost-wakeup window between set() and
clear(), survivable only because the SSE loop re-read on a 30s timeout.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from webcompanion.atomic import write_text_atomic
from webcompanion.paths import VALID_SID_RE

# What `rehydrate()` reports about `sessions.json`. The distinction between
# ABSENT and UNREADABLE is the whole point: an absent file is a first run and
# an empty registry is correct, while an unreadable one means the daemon has
# NO IDEA what is live -- and the stray sweep deletes exactly what no row
# points at. `cleanup.sweep` refuses its stray pass on UNREADABLE.
LOADED = "loaded"
ABSENT = "absent"
UNREADABLE = "unreadable"


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

    def kinds_for_slug(self, key: str) -> list[str]:
        """Every kind in which `key` is a live slug, sorted.

        Exists so an ambiguous slug can be answered with the list of kinds
        it matches rather than a bare 404 -- see server._session. Returns []
        for a sid or for a slug nothing uses.
        """
        with self._lock:
            return sorted({
                str(m.get("kind") or "") for sid, m in self._meta.items()
                if m.get("slug") == key and sid in self._sessions
            } - {""})

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
    @property
    def _lock_file(self) -> Path:
        return self._state_root / "sessions.lock"

    @contextlib.contextmanager
    def _cross_process_lock(self):
        """Serialise read-modify-write of `sessions.json` across PROCESSES.

        `self._lock` is a `threading.Lock` and guards this process's own
        threads only. The daemon and `webcompanion migrate` are two
        processes writing one file; without an flock, whichever writes last
        wins outright and the other's rows are gone -- and a row that is
        gone is a workspace the next startup sweep deletes. `versions.py`
        already locks a version chain this way; the file whose loss deletes
        data had no lock at all.

        The lock lives in a sidecar (`sessions.lock`), never on
        `sessions.json` itself, because `write_text_atomic` replaces that
        path by rename -- a lock held on the old inode would protect a file
        nobody is reading any more.
        """
        self._state_root.mkdir(parents=True, exist_ok=True)
        fd = self._lock_file.open("a")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            fd.close()

    @staticmethod
    def _dirs_live(dirs: dict) -> bool:
        """Every real directory this row names still exists on disk.

        The same strictness `rehydrate` and `cleanup.prune_dead_rows` apply,
        and it is what makes the merge in `persist()` safe: a row this
        process deliberately unregistered was unregistered BECAUSE its
        workspace is gone (`prune_dead_rows`) or was just deleted
        (`expire`), so it fails this check and is not resurrected from disk,
        while a row another process legitimately added is kept.
        """
        real = [v for k, v in dirs.items() if not str(k).startswith("_")]
        return bool(real) and all(Path(p).is_dir() for p in real)

    def _read_file(self, path: Path) -> tuple[dict, str]:
        """(rows, status) for one persisted registry file."""
        try:
            raw = path.read_text()
        except FileNotFoundError:
            return {}, ABSENT
        except OSError:
            return {}, UNREADABLE
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}, UNREADABLE
        if not isinstance(parsed, dict):
            return {}, UNREADABLE
        return parsed, LOADED

    def persist(self) -> None:
        """Write this process's rows, merged with whatever else is on disk.

        NOT a wholesale rewrite from memory. This registry only ever knows
        about the sessions IT created or rehydrated; a row another process
        added in the meantime (a `migrate --apply` run, most of all) is
        still live, and dropping it here deletes its workspace on the next
        startup sweep. So: take the file lock, re-read, union, write.
        """
        self._state_root.mkdir(parents=True, exist_ok=True)
        with self._cross_process_lock():
            with self._lock:
                snapshot = {
                    sid: {k: str(v) for k, v in dirs.items()}
                    for sid, dirs in self._sessions.items()
                }
                meta_snapshot = {sid: dict(m) for sid, m in self._meta.items()}

            on_disk, status = self._read_file(self.sessions_file)
            if status == UNREADABLE:
                # Never overwrite an unreadable registry in place: it is the
                # only record of which workspaces are live, and a human may
                # be able to salvage it. Move it aside first, once.
                self._preserve_unreadable()
            elif status == LOADED:
                for sid, dirs in on_disk.items():
                    if sid in snapshot or not VALID_SID_RE.match(str(sid)):
                        continue
                    if not isinstance(dirs, dict) or not self._dirs_live(dirs):
                        continue
                    snapshot[sid] = {k: str(v) for k, v in dirs.items()}

            disk_meta, meta_status = self._read_file(self.sessions_meta_file)
            if meta_status == LOADED:
                for sid in snapshot:
                    if sid not in meta_snapshot and isinstance(disk_meta.get(sid), dict):
                        meta_snapshot[sid] = disk_meta[sid]

            write_text_atomic(self.sessions_file, json.dumps(snapshot, indent=2))
            write_text_atomic(self.sessions_meta_file,
                              json.dumps(meta_snapshot, indent=2))

    def _preserve_unreadable(self) -> None:
        """Rename an unreadable `sessions.json` aside instead of clobbering
        it. Best-effort: failing to preserve it must not stop the daemon
        from writing a usable registry."""
        dest = self.sessions_file.with_name(
            f"{self.sessions_file.name}.corrupt-{int(time.time())}")
        try:
            os.replace(self.sessions_file, dest)
        except OSError:
            pass

    def rehydrate(self) -> str:
        """Restore rows whose directories still exist, and report what
        `sessions.json` looked like: LOADED, ABSENT, or UNREADABLE.

        The status is not decoration. `Daemon.start()` hands it to
        `cleanup.sweep`, which refuses to delete unregistered workspaces
        when the registry could not be read -- an empty registry then means
        "we do not know what is live", not "nothing is live". Swallowing the
        error and returning, as this used to, made those two
        indistinguishable and deleted every workspace on the next boot.

        Reads absolute paths straight out of sessions.json, so no Config is
        needed here.
        """
        snapshot, status = self._read_file(self.sessions_file)
        if status != LOADED:
            return status
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
        msnap, mstatus = self._read_file(self.sessions_meta_file)
        if mstatus == LOADED:
            with self._lock:
                live = set(self._sessions)
                self._meta.update({
                    sid: m for sid, m in msnap.items()
                    if sid in live and isinstance(m, dict)
                })
        return LOADED

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
