"""The daemon: one threaded HTTP server on loopback, running until stopped.

Deliberately absent, and each absence is load-bearing:

  * No idle shutdown. The five servers this replaces exited after 24 hours
    idle, which under a supervisor becomes a daily restart that drops every
    open SSE stream and every IntelliJ client — and the runtime has no
    reconnect logic.
  * No source fingerprinting and no self-restart. A running daemon cannot
    notice that its own package was upgraded underneath it, so it does not
    pretend to; /health reports its version and the CLI compares.
  * No environment reads. See config.py.
  * No file watching.
"""
from __future__ import annotations

import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from html import escape as _html_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from webcompanion import CONTRACT, __version__
from webcompanion import anchors, cleanup, events, gate, items, paths, stream, threads, uploads
from webcompanion.atomic import write_text_atomic
from webcompanion.config import Config
from webcompanion.registry import Registry

BANNER = f"webcompanion v{__version__}"

# Path patterns for the sid-scoped routes. Kept at module scope, next to the
# dispatch that uses them, rather than folded into a computed dispatch dict —
# the audit that ships with claude-annotate greps for a literal
# `_require_owner()` call sitting directly inside each mutating route's
# handler, so the routing stays a flat if/elif chain rather than a table of
# functions.
_SID_POLL_RE = re.compile(r"^/s/([^/]+)/poll$")
_SID_STREAM_RE = re.compile(r"^/s/([^/]+)/stream$")
_SID_FINISH_RE = re.compile(r"^/s/([^/]+)/api/finish$")
_SID_CANCEL_RE = re.compile(r"^/s/([^/]+)/api/cancel$")
_SID_ITEMS_RE = re.compile(r"^/s/([^/]+)/items$")
_SID_ITEM_RE = re.compile(r"^/s/([^/]+)/items/(.+)$")
_SID_ASSETS_REGISTER_RE = re.compile(r"^/s/([^/]+)/api/assets$")
_SID_ASSET_RE = re.compile(r"^/s/([^/]+)/assets/(.+)$")
_SID_UPLOAD_RE = re.compile(r"^/s/([^/]+)/api/upload$")
_SID_SUBMIT_RE = re.compile(r"^/s/([^/]+)/api/submit$")
_SID_THREADS_RE = re.compile(r"^/s/([^/]+)/threads$")
_SID_THREAD_RE = re.compile(r"^/s/([^/]+)/threads/(.+)$")
_SID_THREAD_DELETE_RE = re.compile(r"^/s/([^/]+)/api/threads/delete$")
_SID_ROOT_RE = re.compile(r"^/s/([^/]+)/$")

# finished/cancelled are FILES in state_dir, not server memory: the daemon
# restarts on every package upgrade, and Task 15's watcher (and Task 12's
# stream.serve is_terminal check) run as a SEPARATE process that polls the
# filesystem, not this process's memory. A dict here would resurrect every
# finished session on restart and would be invisible to that watcher.
_FINISHED_MARKER = "finished"
_CANCELLED_MARKER = "cancelled"

# The durable record of a startup sweep that failed or was refused. `doctor`
# reads this exact filename.
SWEEP_MARKER = "startup_sweep_failed.json"


def _mark(state_dir: Path, name: str) -> None:
    write_text_atomic(Path(state_dir) / name, "")


def _is_marked(state_dir: Path, name: str) -> bool:
    return (Path(state_dir) / name).exists()


def _is_terminal(state_dir: Path) -> bool:
    """Finished or cancelled — either one ends a stream. Reads the same
    files a separate watcher process polls; there is no daemon-memory
    shortcut, because a restart must see the same answer the watcher does."""
    return _is_marked(state_dir, _FINISHED_MARKER) or _is_marked(state_dir, _CANCELLED_MARKER)


# `webcompanion watch` writes this by atomic rename on every poll. It is a
# FILE for the same reason finished/cancelled are: the watcher is a separate
# OS process, so the daemon cannot see its memory and a dict here would
# report None forever -- which is exactly what /poll did.
_HEARTBEAT_FILE = "watcher_heartbeat"


def _watcher_seen_at(state_dir: Path) -> int | None:
    """The unix time of the watcher's last heartbeat, or None if no watcher
    has ever beaten for this session. Never raises: a half-written or
    hand-edited file reads as "no watcher", not as a 500 from /poll."""
    try:
        raw = (Path(state_dir) / _HEARTBEAT_FILE).read_text()
    except OSError:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


# Registered renderer roots are a FILE in the session's workspace, not
# daemon memory -- the same fix Task 10 applied to finished/cancelled, for
# the same reason. This daemon restarts on every package upgrade; a dict
# here would 404 every open browser tab's stylesheet and script right after
# an upgrade, with no visible cause. No in-memory cache is kept alongside
# it either: a cache and the disk state can disagree after a restart, and
# the cache would silently win.
_ASSETS_MARKER = "assets.json"


def _static_file(name: str):
    """Context manager yielding a real filesystem Path for a packaged static
    asset (core.js, shell.html). importlib.resources.as_file works whether
    the package is an installed wheel or the zipapp the service ships as in
    Task 16 -- Path(__file__).parent does not survive the latter."""
    return as_file(files("webcompanion").joinpath("static", name))


def _write_asset_root(base: Path, static_root: str, entry: str | None) -> None:
    write_text_atomic(Path(base) / _ASSETS_MARKER,
                       json.dumps({"static_root": static_root, "entry": entry}))


def _read_asset_root(base: Path) -> dict | None:
    """The registered {static_root, entry} for this session, or None.

    Re-validates static_root is still an existing directory on every call --
    a persisted value can point at a directory since deleted or replaced,
    and a stale registration must read as "nothing registered", never raise
    inside the request handler.
    """
    try:
        raw = json.loads((Path(base) / _ASSETS_MARKER).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    static_root = raw.get("static_root")
    if not isinstance(static_root, str) or not Path(static_root).is_dir():
        return None
    entry = raw.get("entry")
    return {"root": static_root, "entry": entry if isinstance(entry, str) else None}


def _editor_command(target: Path, line: int | None) -> list[str]:
    """Ported from skills/_shared/web_companion/server.py:897 unchanged: an
    IDE launcher wins when present (and takes a line number), otherwise the
    platform's generic file opener."""
    launcher = shutil.which("idea")
    if launcher and line:
        return [launcher, "--line", str(line), str(target)]
    if launcher:
        return [launcher, str(target)]
    if sys.platform == "darwin":
        return ["open", str(target)]
    return ["xdg-open", str(target)]


class Daemon:
    def __init__(self, cfg: Config, state_root: Path | None = None):
        self.cfg = cfg
        self.state_root = Path(state_root) if state_root else paths.state_root()
        self.registry = Registry(self.state_root)
        self.started_at = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The daemon's own base URL, built from the port it actually bound.

        Tests start on port=0 (an ephemeral port chosen by the OS); a `url`
        built from `cfg.port` would report `:0` forever.
        """
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _record_sweep_problem(self, **fields) -> None:
        """Drop the durable marker `doctor` reads. A stderr line alone means
        nobody learns cleanup stopped running -- launchd's log rotates and
        nobody reads it until something else breaks."""
        try:
            write_text_atomic(
                self.state_root / SWEEP_MARKER,
                json.dumps({"when": time.time(), **fields}, indent=2),
            )
        except OSError:
            pass

    def _clear_sweep_problem(self) -> None:
        """Remove the marker after a boot whose sweep ran clean.

        The marker is durable ON PURPOSE, but durable is not permanent: it
        described one boot, and `doctor` exits 1 while it exists. Left
        forever, one refused boot makes `doctor` fail on every healthy
        machine after it, and its own advice -- "fix the cause and restart"
        -- is then advice that cannot work, because restarting is exactly
        what does not clear it. A boot that rehydrated its registry, swept
        without refusing, and preserved nothing has demonstrated the cause
        is gone; that is the only thing allowed to clear it.
        """
        try:
            (self.state_root / SWEEP_MARKER).unlink()
        except OSError:
            pass

    def start(self) -> None:
        # The status matters as much as the rows: an unreadable sessions.json
        # must not read as "no sessions exist", because the stray sweep
        # deletes precisely what no row points at. See cleanup.sweep.
        registry_status = self.registry.rehydrate()
        # A cleanup failure must never keep the socket from binding: a
        # PermissionError or a directory vanishing mid-scan here, left
        # unguarded, means launchd's KeepAlive respawns forever with every
        # skill seeing connection refused and nothing saying why. A daemon
        # that skips a sweep is strictly better than one that refuses to
        # start.
        problem: dict = {}
        try:
            result = cleanup.sweep(self.cfg, self.registry,
                                   registry_status=registry_status)
        except Exception as exc:
            print("webcompanion: startup cleanup sweep failed, continuing:",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            problem["error"] = f"{type(exc).__name__}: {exc}"
        else:
            if result.get("strays_refused"):
                print(f"webcompanion: refused the startup stray sweep: "
                      f"{result['strays_refused']}", file=sys.stderr)
                problem["refused"] = result["strays_refused"]
        # persist() reports whether it had to move an unreadable
        # sessions.json aside. That is a data-loss-adjacent event a human has
        # to be told about, so it goes in the same durable marker -- and it
        # keeps the marker standing on a boot where the sweep itself was
        # clean but the registry was not.
        preserved = self.registry.persist()
        if preserved is not None:
            print(f"webcompanion: {self.registry.sessions_file} could not be "
                  f"parsed; preserved it as {preserved}", file=sys.stderr)
            problem["preserved_unreadable_registry"] = str(preserved)
        if problem:
            self._record_sweep_problem(**problem)
        else:
            self._clear_sweep_problem()
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.cfg.bind, self.cfg.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)



def _make_handler(daemon: Daemon):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = BANNER

        def log_message(self, fmt, *args):
            pass  # the service log is for failures, not an access log

        # ── plumbing ────────────────────────────────────────────────────
        def _json(self, status: int, obj) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _html(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_file(self, path: Path) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self._text(404, "no such file")
                return
            ctype, _ = mimetypes.guess_type(str(path))
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if n <= 0:
                return {}
            try:
                parsed = json.loads(self.rfile.read(n))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _require_owner(self) -> bool:
            if gate.is_owner(self, daemon.cfg.token):
                return True
            self._text(403, "forbidden")
            return False

        def _contract_ok(self) -> bool:
            ok, message = gate.check_contract(self)
            if not ok:
                self._text(426, message)
            return ok

        def _session(self, sid: str):
            """Resolve a sid or slug, honouring `?kind=` when given.

            A slug is unique within a kind, not across them, and three kinds
            sharing one slug is real in practice. `resolve` answers None for
            an ambiguous slug, which used to surface as `404 no such
            session` -- a status that reads as "it is gone" for something
            that very much exists, three times over. `?kind=` picks one, and
            a still-ambiguous slug is a 409 that NAMES the candidates so the
            caller can retry without guessing.
            """
            kind = (parse_qs(urlsplit(self.path).query).get("kind") or [None])[0]
            resolved = daemon.registry.resolve(sid, kind=kind)
            if resolved is not None:
                return resolved, daemon.registry.lookup(resolved)
            kinds = daemon.registry.kinds_for_slug(sid)
            if len(kinds) > 1:
                self._text(409, "the slug %r exists in more than one kind (%s); "
                                "add ?kind=<kind> to say which"
                                % (sid, ", ".join(kinds)))
            else:
                self._text(404, "no such session")
            return None, None

        def _row(self, sid: str) -> dict:
            meta = daemon.registry.get_meta(sid)
            return {
                "sid": sid,
                "slug": meta.get("slug", ""),
                "kind": meta.get("kind", ""),
                "cwd": meta.get("cwd", ""),
                "title": meta.get("title", ""),
                "url": f"{daemon.url}/s/{sid}/",
            }

        # ── dispatch ────────────────────────────────────────────────────
        def do_GET(self):
            if not self._contract_ok():
                return
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/health":
                return self._health()
            if path == "/api/whoami":
                return self._whoami()
            if path == "/api/sessions":
                return self._list_sessions(parse_qs(parsed.query))
            if path == "/_wc/core.js":
                return self._core_js()
            m = _SID_POLL_RE.match(path)
            if m:
                return self._poll(m.group(1))
            m = _SID_STREAM_RE.match(path)
            if m:
                return self._stream(m.group(1))
            m = _SID_ITEMS_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._list_items(dirs)
            m = _SID_ITEM_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._get_item(resolved, dirs, unquote(m.group(2)))
            m = _SID_THREADS_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._list_threads(dirs)
            m = _SID_THREAD_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._get_thread(dirs, unquote(m.group(2)))
            m = _SID_ASSET_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._get_asset(dirs, unquote(m.group(2)))
            m = _SID_ROOT_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._get_shell(resolved, dirs)
            self._text(404, "not found")

        def do_POST(self):
            if not self._contract_ok():
                return
            path = urlsplit(self.path).path
            if path == "/api/sessions":
                return self._create_session()
            if path == "/api/open":
                return self._open_in_editor()
            m = _SID_FINISH_RE.match(path)
            if m:
                return self._finish(m.group(1))
            m = _SID_CANCEL_RE.match(path)
            if m:
                return self._cancel(m.group(1))
            m = _SID_ASSETS_REGISTER_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._register_assets(dirs)
            m = _SID_UPLOAD_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._upload(dirs)
            m = _SID_SUBMIT_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._submit(resolved, dirs)
            m = _SID_THREAD_DELETE_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._delete_thread(resolved, dirs)
            m = _SID_THREAD_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._append_to_thread(resolved, dirs, unquote(m.group(2)))
            self._text(404, "not found")

        def do_PUT(self):
            if not self._contract_ok():
                return
            path = urlsplit(self.path).path
            m = _SID_ITEM_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._put_item(resolved, dirs, unquote(m.group(2)))
            self._text(404, "not found")

        def do_PATCH(self):
            if not self._contract_ok():
                return
            path = urlsplit(self.path).path
            m = _SID_ITEMS_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._patch_items(resolved, dirs)
            self._text(404, "not found")

        def do_DELETE(self):
            if not self._contract_ok():
                return
            path = urlsplit(self.path).path
            m = _SID_ITEM_RE.match(path)
            if m:
                resolved, dirs = self._session(m.group(1))
                if resolved is None:
                    return
                return self._delete_item(resolved, dirs, unquote(m.group(2)))
            self._text(404, "not found")

        # ── routes ──────────────────────────────────────────────────────
        def _health(self) -> None:
            self._json(200, {
                "banner": BANNER,
                "contract": CONTRACT,
                "version": __version__,
                "uptime": time.time() - daemon.started_at,
                "sessions": len(daemon.registry.items()),
            })

        def _whoami(self) -> None:
            self._json(200, {"writable": gate.is_owner(self, daemon.cfg.token)})

        def _list_sessions(self, query: dict) -> None:
            scope = (query.get("scope") or [""])[0]
            kind = (query.get("kind") or [None])[0]
            cwd = (query.get("cwd") or [None])[0]
            if scope == "all":
                if not self._require_owner():
                    return
                rows = [self._row(sid) for sid, _ in daemon.registry.list_all()]
                self._json(200, rows)
                return
            if not cwd:
                self._text(400, "cwd is required")
                return
            rows = [self._row(sid) for sid, _ in daemon.registry.find(cwd=cwd, kind=kind)]
            self._json(200, rows)

        def _finish(self, sid: str) -> None:
            if not self._require_owner():
                return
            resolved, dirs = self._session(sid)
            if resolved is None:
                return
            _mark(Path(dirs["state_dir"]), _FINISHED_MARKER)
            daemon.registry.note_change(resolved)
            self._json(200, {"ok": True})

        def _cancel(self, sid: str) -> None:
            if not self._require_owner():
                return
            resolved, dirs = self._session(sid)
            if resolved is None:
                return
            _mark(Path(dirs["state_dir"]), _CANCELLED_MARKER)
            daemon.registry.note_change(resolved)
            self._json(200, {"ok": True})

        def _supersede_siblings(self, kind: str, cwd: str, sid: str) -> None:
            """End the caller's other live sessions of this kind and cwd.

            Per-request, not a class attribute: this is what replaces the
            per-skill class flag the five servers each set (annotate ends
            its older sessions, deck does not) with one payload field any
            caller can opt into. Scoped to BOTH kind and cwd: a session of a
            different kind, or the same kind in a different cwd, must be
            left alone, or this would silently end a user's unrelated work.
            """
            for other_sid, other_dirs in daemon.registry.find(cwd=cwd, kind=kind):
                if other_sid == sid:
                    continue
                _mark(Path(other_dirs["state_dir"]), _FINISHED_MARKER)
                daemon.registry.note_change(other_sid)

        def _create_session(self) -> None:
            if not self._require_owner():
                return
            payload = self._body()
            kind = str(payload.get("kind") or "")
            cwd = str(payload.get("cwd") or "")
            if not paths.VALID_KIND_RE.match(kind):
                self._text(400, "kind is required and must match [a-z][a-z0-9_-]*")
                return
            if not cwd:
                self._text(400, "cwd is required")
                return
            sid = daemon.registry.make_sid()
            try:
                dirs = paths.make_session_dirs(daemon.cfg, kind, sid)
            except ValueError as e:
                self._text(400, str(e))
                return
            slug = daemon.registry.create(
                kind, sid, dirs,
                {"title": str(payload.get("title") or "")},
                cwd, str(payload.get("slug") or ""),
            )
            paths.write_marker(paths.base_of(dirs), sid, kind, cwd)
            # supersede replaces the per-skill class attribute the five
            # servers each set: annotate ends its older sessions for the same
            # cwd, deck does not.
            if payload.get("supersede"):
                self._supersede_siblings(kind, cwd, sid)
            daemon.registry.persist()
            self._json(201, {
                "sid": sid, "slug": slug, "kind": kind,
                "url": f"{daemon.url}/s/{sid}/",
                "token": daemon.cfg.token,
            })

        # ── items ───────────────────────────────────────────────────────
        def _list_items(self, dirs: dict) -> None:
            self._json(200, items.snapshot(dirs["items_dir"]))

        def _get_item(self, sid: str, dirs: dict, anchor: str) -> None:
            body = items.load_one(dirs["items_dir"], anchor)
            if body is None:
                self._text(404, "no such item")
                return
            versions = items.versions_of(dirs["items_dir"])
            out = {"body": body, "version": versions.get(anchor, 1)}
            # A row with no _cwd cannot resolve code anchors -- and reaching
            # for the key anyway raised KeyError inside the handler, i.e. a
            # 500 with a traceback for what is a malformed session row.
            cwd = dirs.get("_cwd")
            if not str(cwd or "").strip():
                self._text(400, "this session has no workspace root recorded")
                return
            # Resolved HERE, not at push time. The client edits its repository
            # while the session is open; an anchor captured at push is wrong
            # within a turn.
            resolved = anchors.resolve_all(body, Path(cwd))
            if resolved:
                out["code"] = resolved
            self._json(200, out)

        def _put_item(self, sid: str, dirs: dict, anchor: str) -> None:
            if not self._require_owner():
                return
            try:
                items.put(dirs["items_dir"], anchor, self._body())
            except ValueError as e:
                self._text(400, str(e))
                return
            daemon.registry.note_change(sid)
            self._json(200, {"ok": True})

        def _patch_items(self, sid: str, dirs: dict) -> None:
            if not self._require_owner():
                return
            payload = self._body()
            bodies = payload.get("items")
            if not isinstance(bodies, dict):
                self._text(400, "items must be an object of anchor -> body")
                return
            replace = bool(payload.get("replace", False))
            try:
                items.put_many(dirs["items_dir"], bodies, replace=replace)
            except ValueError as e:
                self._text(400, str(e))
                return
            daemon.registry.note_change(sid)
            self._json(200, {"ok": True})

        def _delete_item(self, sid: str, dirs: dict, anchor: str) -> None:
            if not self._require_owner():
                return
            items.delete(dirs["items_dir"], anchor)
            daemon.registry.note_change(sid)
            self._json(200, {"ok": True})

        # ── threads ─────────────────────────────────────────────────────
        # The thread store has been ported, tested and flock-serialised
        # since Task 4, and Task 17 relocated 95 real threads to preserve
        # them -- but until these four routes existed nothing could read or
        # append one, while contract 1 already defined a Thread, returned
        # `threads: {anchor: version}` from /poll, and documented
        # thread-changed / thread-deleted frames. Adding them after 1.0.0
        # would have meant a contract bump across four artifacts.
        def _list_threads(self, dirs: dict) -> None:
            self._json(200, threads.snapshot(dirs["threads_dir"]))

        def _get_thread(self, dirs: dict, anchor: str) -> None:
            if not threads.valid_anchor(anchor):
                self._text(400, "invalid anchor")
                return
            # An anchor nobody has commented on yet is not an error: a
            # client asks for the thread of every region it renders, and a
            # 404 for each un-commented one would be noise. version 0 with
            # no messages is the "nothing here yet" answer.
            self._json(200, threads.load(dirs["threads_dir"], anchor))

        def _append_to_thread(self, sid: str, dirs: dict, anchor: str) -> None:
            if not self._require_owner():
                return
            if not threads.valid_anchor(anchor):
                self._text(400, "invalid anchor")
                return
            payload = self._body()
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                self._text(400, "text is required")
                return
            msg = {k: v for k, v in payload.items()
                   if k not in ("title", "anchor_text")}
            msg.setdefault("role", "agent")
            msg.setdefault("ts", int(time.time()))
            title = payload.get("title")
            anchor_text = payload.get("anchor_text")
            if isinstance(anchor_text, str) and anchor_text:
                threads.set_anchor_text_if_absent(
                    dirs["threads_dir"], anchor, anchor_text)
            appended = threads.append_message(
                dirs["threads_dir"], anchor, msg,
                title=title if isinstance(title, str) else None)
            # Bumped whether or not the message was new: anchor_text may
            # have been set, and a no-op bump costs one SSE wakeup while a
            # missed one costs a client that never redraws.
            daemon.registry.note_change(sid)
            thread = threads.load(dirs["threads_dir"], anchor)
            self._json(200, {"appended": appended, "version": thread["version"]})

        def _delete_thread(self, sid: str, dirs: dict) -> None:
            if not self._require_owner():
                return
            anchor = self._body().get("anchor")
            if not isinstance(anchor, str) or not threads.valid_anchor(anchor):
                self._text(400, "anchor is required and must be a valid anchor")
                return
            deleted = threads.delete(dirs["threads_dir"], anchor)
            daemon.registry.note_change(sid)
            self._json(200, {"deleted": deleted})

        # ── assets and the page ────────────────────────────────────────────
        def _register_assets(self, dirs: dict) -> None:
            if not self._require_owner():
                return
            payload = self._body()
            static_root = payload.get("static_root")
            if not isinstance(static_root, str) or not static_root:
                self._text(400, "static_root is required")
                return
            try:
                root = Path(static_root).resolve()
            except (OSError, ValueError) as e:
                self._text(400, "static_root could not be resolved (%s)"
                           % e.__class__.__name__)
                return
            if not root.is_dir():
                self._text(400, "static_root must be an existing directory")
                return
            entry = payload.get("entry")
            entry = entry if isinstance(entry, str) and entry else None
            # Persisted to the session's own workspace, not daemon memory:
            # this is an always-on service restarted on every package
            # upgrade, and a dict here would 404 every open tab's stylesheet
            # and script right after one, same as Task 10's finished/
            # cancelled fix. No in-memory cache is kept beside it either --
            # after a restart a cache and the disk state can disagree, and
            # the cache would silently win.
            _write_asset_root(paths.base_of(dirs), str(root), entry)
            self._json(200, {"ok": True})

        def _get_asset(self, dirs: dict, relpath: str) -> None:
            # Re-read (and re-validate) from disk on every request, never
            # from a cache -- see the comment in _register_assets.
            info = _read_asset_root(paths.base_of(dirs))
            if info is None:
                self._text(404, "no renderer registered for this session")
                return
            root = Path(info["root"])
            try:
                # resolve() follows symlinks BEFORE the containment test --
                # a link inside the bundle pointing out of it must not
                # smuggle a file through. The root itself is re-read from
                # disk above, so this check runs against the CURRENT value,
                # never one trusted safe merely because it validated once at
                # registration time.
                target = (root / relpath).resolve()
            except (OSError, ValueError):
                self._text(403, "forbidden")
                return
            if not target.is_relative_to(root):
                self._text(403, "forbidden")
                return
            if not target.is_file():
                self._text(404, "no such asset")
                return
            self._serve_file(target)

        def _core_js(self) -> None:
            with _static_file("core.js") as p:
                self._serve_file(p)

        def _get_shell(self, sid: str, dirs: dict) -> None:
            # The packaged shell.html template, with its two placeholders
            # filled in: {{TITLE}} from the session's own metadata, {{ENTRY}}
            # with the registered renderer's script tag (or nothing, when no
            # renderer has registered yet) -- a session always gets SOME
            # page back, and it always names the daemon's runtime.
            info = _read_asset_root(paths.base_of(dirs))
            entry_tag = ""
            if info and info.get("entry"):
                # ESCAPED, exactly like the title two lines below. `entry` is
                # whatever a POST to /api/assets stored, so an unescaped
                # interpolation here closes the src attribute and the script
                # tag and runs attacker JS on the daemon's own origin -- with
                # the write token in sessionStorage for that origin. quote=True
                # is what handles the `"` that does the closing.
                entry_tag = ('<script type="module" src="assets/%s"></script>'
                             % _html_escape(info["entry"], quote=True))
            title = daemon.registry.get_meta(sid).get("title") or "webcompanion"
            with _static_file("shell.html") as p:
                template = p.read_text()
            html = (template.replace("{{TITLE}}", _html_escape(title))
                             .replace("{{ENTRY}}", entry_tag))
            self._html(200, html)

        # ── uploads, submit, poll ────────────────────────────────────────
        def _upload(self, dirs: dict) -> None:
            if not self._require_owner():
                return
            uploads.handle(self, dirs)

        def _submit(self, sid: str, dirs: dict) -> None:
            if not self._require_owner():
                return
            payload = self._body()
            anchor = payload.get("anchor")
            text = payload.get("text")
            if not isinstance(anchor, str) or not items.valid_anchor(anchor):
                self._text(400, "anchor is required and must be a valid anchor")
                return
            if not isinstance(text, str) or not text.strip():
                self._text(400, "text is required")
                return
            image_refs = payload.get("images") or []
            if image_refs and not uploads.images_ok(image_refs, dirs["state_dir"]):
                self._text(400, "images must reference this session's uploads")
                return
            event_id = events.append(dirs["events_dir"], {
                "anchor": anchor, "text": text, "images": image_refs,
            })
            daemon.registry.note_change(sid)
            self._json(202, {"event_id": event_id})

        def _poll(self, sid: str) -> None:
            resolved, dirs = self._session(sid)
            if resolved is None:
                return
            state_dir = Path(dirs["state_dir"])
            self._json(200, {
                "finished": _is_marked(state_dir, _FINISHED_MARKER),
                "cancelled": _is_marked(state_dir, _CANCELLED_MARKER),
                "watcher_seen_at": _watcher_seen_at(state_dir),
                "items": items.versions_of(dirs["items_dir"]),
                "threads": threads.list_versions(dirs["threads_dir"]),
            })

        def _stream(self, sid: str) -> None:
            resolved, dirs = self._session(sid)
            if resolved is None:
                return
            stream.serve(self, resolved, dirs, registry=daemon.registry,
                         is_terminal=_is_terminal)

        # ── open in editor ──────────────────────────────────────────────
        def _path_in_any_session_cwd(self, target: Path) -> bool:
            """One daemon now holds sessions from every project on the
            machine, so this is the whole defence for the daemon's only
            subprocess capability."""
            for _, dirs in daemon.registry.items():
                cwd = dirs.get("_cwd")
                if not cwd:
                    continue
                try:
                    root_real = Path(cwd).resolve()
                except (OSError, ValueError):
                    continue
                if target.is_relative_to(root_real):
                    return True
            return False

        def _open_in_editor(self) -> None:
            if not self._require_owner():
                return
            payload = self._body()
            file_ = payload.get("file")
            if not isinstance(file_, str) or not file_:
                self._text(400, "file is required")
                return
            line = payload.get("line")
            line = line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None
            try:
                target = Path(file_).resolve()
            except (OSError, ValueError) as e:
                self._text(400, "path could not be resolved (%s)" % e.__class__.__name__)
                return
            if not self._path_in_any_session_cwd(target):
                self._text(403, "forbidden: outside every session's workspace")
                return
            if not target.is_file():
                self._text(404, "no such file")
                return
            try:
                subprocess.Popen(_editor_command(target, line), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                self._text(500, "could not launch the editor (%s)" % e.__class__.__name__)
                return
            self._json(200, {"opened": str(target), "line": line})

    return Handler


def make_server(cfg: Config) -> Daemon:
    return Daemon(cfg)


def serve_forever(cfg: Config) -> int:
    daemon = make_server(cfg)
    daemon.start()
    try:
        assert daemon._thread is not None
        while daemon._thread.is_alive():
            daemon._thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0
