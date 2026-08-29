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
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from webcompanion import CONTRACT, __version__
from webcompanion import anchors, events, gate, items, paths, threads, uploads
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
_SID_FINISH_RE = re.compile(r"^/s/([^/]+)/api/finish$")
_SID_CANCEL_RE = re.compile(r"^/s/([^/]+)/api/cancel$")


class Daemon:
    def __init__(self, cfg: Config, state_root: Path | None = None):
        self.cfg = cfg
        self.state_root = Path(state_root) if state_root else paths.state_root()
        self.registry = Registry(self.state_root)
        self.started_at = time.time()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # Finish/cancel/poll state. This is server-level, not registry state:
        # the registry only knows identity and directories, and exposes no
        # setter for arbitrary meta — deliberately, since sid/slug/kind/cwd
        # are the only things every kind agrees mean the same thing.
        self._flags: dict[str, dict] = {}
        self._flags_lock = threading.Lock()

    @property
    def url(self) -> str:
        """The daemon's own base URL, built from the port it actually bound.

        Tests start on port=0 (an ephemeral port chosen by the OS); a `url`
        built from `cfg.port` would report `:0` forever.
        """
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.registry.rehydrate()
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

    # ── finish / cancel / poll state ────────────────────────────────────
    _DEFAULT_FLAGS = {"finished": False, "cancelled": False, "watcher_seen_at": None}

    def init_flags(self, sid: str) -> None:
        with self._flags_lock:
            self._flags[sid] = dict(self._DEFAULT_FLAGS)

    def set_finished(self, sid: str) -> None:
        with self._flags_lock:
            self._flags.setdefault(sid, dict(self._DEFAULT_FLAGS))
            self._flags[sid]["finished"] = True
        self.registry.note_change(sid)

    def set_cancelled(self, sid: str) -> None:
        with self._flags_lock:
            self._flags.setdefault(sid, dict(self._DEFAULT_FLAGS))
            self._flags[sid]["cancelled"] = True
        self.registry.note_change(sid)

    def get_flags(self, sid: str) -> dict:
        with self._flags_lock:
            return dict(self._flags.get(sid, self._DEFAULT_FLAGS))


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
            resolved = daemon.registry.resolve(sid)
            if resolved is None:
                self._text(404, "no such session")
                return None, None
            return resolved, daemon.registry.lookup(resolved)

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
            m = _SID_POLL_RE.match(path)
            if m:
                return self._poll(m.group(1))
            self._text(404, "not found")

        def do_POST(self):
            if not self._contract_ok():
                return
            path = urlsplit(self.path).path
            if path == "/api/sessions":
                return self._create_session()
            m = _SID_FINISH_RE.match(path)
            if m:
                return self._finish(m.group(1))
            m = _SID_CANCEL_RE.match(path)
            if m:
                return self._cancel(m.group(1))
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

        def _poll(self, sid: str) -> None:
            resolved, _ = self._session(sid)
            if resolved is None:
                return
            self._json(200, daemon.get_flags(resolved))

        def _finish(self, sid: str) -> None:
            if not self._require_owner():
                return
            resolved, _ = self._session(sid)
            if resolved is None:
                return
            daemon.set_finished(resolved)
            self._json(200, {"ok": True})

        def _cancel(self, sid: str) -> None:
            if not self._require_owner():
                return
            resolved, _ = self._session(sid)
            if resolved is None:
                return
            daemon.set_cancelled(resolved)
            self._json(200, {"ok": True})

        def _supersede_siblings(self, kind: str, cwd: str, sid: str) -> None:
            """End the caller's other live sessions of this kind and cwd.

            Per-request, not a class attribute: this is what replaces the
            per-skill class flag the five servers each set (annotate ends
            its older sessions, deck does not) with one payload field any
            caller can opt into.
            """
            for other_sid, _ in daemon.registry.find(cwd=cwd, kind=kind):
                if other_sid == sid:
                    continue
                daemon.set_finished(other_sid)

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
            daemon.init_flags(sid)
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
