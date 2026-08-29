"""The daemon's only client. Every command module talks to the daemon
through a `Client`, never through hand-rolled `urllib` calls or a
hand-copied route string -- that duplication is exactly what stranded five
skills on five slightly-different HTTP conventions before this package
existed.

Three exceptions cover every way a call can fail, and `commands/_common.py`
turns each into one of the three diagnostic messages a user needs:

  * `DaemonUnreachable` -- refused, timed out, or any other transport
    failure. The daemon is not answering; the CLI never starts it.
  * `ContractMismatch` -- a 426. One side is running an old version; the
    daemon's own message says which one.
  * `HttpError` -- any other non-2xx response, carrying the status and body
    so a caller can show something more specific than "it failed".
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from webcompanion import CONTRACT
from webcompanion.gate import CONTRACT_HEADER, WRITE_TOKEN_HEADER

REQUEST_TIMEOUT_SECONDS = 10


class DaemonUnreachable(Exception):
    """The daemon did not answer at all: connection refused, DNS failure,
    a timeout -- anything below the HTTP layer."""

    def __init__(self, url: str, log_path: str = ""):
        super().__init__(f"could not reach the daemon at {url}")
        self.url = url
        self.log_path = log_path


class ContractMismatch(Exception):
    """The daemon answered 426. Its own message names which side is old --
    see gate.check_contract -- so this carries that message verbatim rather
    than composing a new one that could drift from it."""


class HttpError(Exception):
    """Any other non-2xx response."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def _log_path() -> str:
    # Where install-service points launchd/systemd's stdout/stderr. Named
    # here, not imported from an install-service module, because a client
    # error path must never import the module that knows how to install
    # things -- see "The CLI NEVER starts the daemon" in the task brief.
    return str(Path("~/.claude/webcompanion/webcompanion.log").expanduser())


class Client:
    """A thin wrapper over `urllib.request` speaking the webcompanion HTTP
    contract: the contract header and the write token go on every call, so
    no command module has to remember either."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ── transport ───────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body=None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # Read as a plain module global (not `webcompanion.client.CONTRACT`
        # qualified) so a caller -- notably the test suite -- can monkeypatch
        # this module's CONTRACT and have it take effect immediately.
        req.add_header(CONTRACT_HEADER, str(CONTRACT))
        if self.token:
            req.add_header(WRITE_TOKEN_HEADER, self.token)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as r:
                raw = r.read().decode("utf-8")
                parsed = json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
                return r.status, parsed
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            if e.code == 426:
                raise ContractMismatch(raw) from None
            raise HttpError(e.code, raw) from None
        except (urllib.error.URLError, OSError) as e:
            raise DaemonUnreachable(self.base_url, _log_path()) from e

    # ── routes ──────────────────────────────────────────────────────────
    def health(self) -> dict:
        _, body = self._request("GET", "/health")
        return body if isinstance(body, dict) else {}

    def create(self, kind: str, cwd: str, title: str = "", slug: str = "",
               supersede: bool = False) -> dict:
        payload = {"kind": kind, "cwd": cwd, "title": title, "slug": slug,
                   "supersede": supersede}
        _, body = self._request("POST", "/api/sessions", payload)
        return body

    def put_items(self, sid: str, bodies: dict, replace: bool = False) -> None:
        self._request("PATCH", f"/s/{sid}/items",
                       {"items": bodies, "replace": replace})

    def put_item(self, sid: str, anchor: str, body) -> None:
        quoted = urllib.parse.quote(anchor, safe="")
        self._request("PUT", f"/s/{sid}/items/{quoted}", body)

    def register_assets(self, sid: str, static_root: str, entry: str | None = None) -> None:
        self._request("POST", f"/s/{sid}/api/assets",
                       {"static_root": static_root, "entry": entry})

    def finish(self, sid: str) -> None:
        self._request("POST", f"/s/{sid}/api/finish")

    def cancel(self, sid: str) -> None:
        self._request("POST", f"/s/{sid}/api/cancel")

    def events(self, sid: str) -> list[dict]:
        """Pending (un-acked) events queued for `sid`, oldest first.

        There is no HTTP route for this -- the daemon never exposed one,
        because the only consumer, `watch`, runs on the same host as the
        daemon and reads the queue directly off disk the way `watcher.sh`
        always did (atomic-rename heartbeats and ack files are filesystem
        operations with no HTTP equivalent). This method exists for the
        rest of the `Client` interface and for callers that just want to
        inspect the queue, resolving `sid` through the daemon's own
        session registry file rather than duplicating its layout rules.
        """
        from webcompanion import paths

        sessions_file = paths.state_root() / "sessions.json"
        try:
            snapshot = json.loads(sessions_file.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        dirs = snapshot.get(sid)
        if not isinstance(dirs, dict) or "events_dir" not in dirs:
            return []
        events_dir = Path(dirs["events_dir"])
        consumed_dir = Path(dirs.get("consumed_dir", ""))
        out = []
        for p in sorted(events_dir.glob("*.json")):
            event_id = p.stem
            if consumed_dir and (consumed_dir / f"{event_id}.ack").exists():
                continue
            try:
                payload = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"event_id": event_id, "payload": payload})
        return out
