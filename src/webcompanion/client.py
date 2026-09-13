"""The daemon's only client. Every command module talks to the daemon
through a `Client`, never through hand-rolled `urllib` calls or a
hand-copied route string -- that duplication is exactly what stranded five
skills on five slightly-different HTTP conventions before this package
existed.

Four exceptions cover every way a call can fail, and `commands/_common.py`
turns each into one of the diagnostic messages a user needs:

  * `DaemonNotConfigured` -- there is no config file, so this machine has
    no daemon of its own to talk to. Distinct from `DaemonUnreachable`:
    unreachable means "installed, not answering", this means "never
    installed". Raised before any socket is opened, because the default
    host and port would otherwise point a client with no configuration at
    whatever happens to be listening -- and loopback callers are trusted as
    owners, so it would not merely read, it would write.
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


class DaemonNotConfigured(Exception):
    """No config file, so there is no daemon this machine has been told about.

    `config.load()` deliberately falls back to bare defaults for a missing
    file -- that is right for the daemon's own boot, which has to mint a
    config before one exists. It is wrong for a client: it silently aims
    every request at the default port, and since the daemon trusts any
    loopback caller as its owner, a client that was never configured could
    create and overwrite sessions on a daemon it was never pointed at.
    """

    def __init__(self, path):
        super().__init__(
            f"no webcompanion configuration at {path}; this machine has no "
            f"companion service installed")
        self.path = path


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

    def get_item(self, sid: str, anchor: str) -> dict:
        quoted = urllib.parse.quote(anchor, safe="")
        _, body = self._request("GET", f"/s/{sid}/items/{quoted}")
        return body if isinstance(body, dict) else {}

    def list_items(self, sid: str) -> dict:
        _, body = self._request("GET", f"/s/{sid}/items")
        return body if isinstance(body, dict) else {}

    def get_thread(self, sid: str, anchor: str) -> dict:
        quoted = urllib.parse.quote(anchor, safe="")
        _, body = self._request("GET", f"/s/{sid}/threads/{quoted}")
        return body if isinstance(body, dict) else {"anchor": anchor, "version": 0, "messages": []}

    def append_thread(self, sid: str, anchor: str, text: str, role: str = "agent") -> dict:
        quoted = urllib.parse.quote(anchor, safe="")
        _, body = self._request("POST", f"/s/{sid}/threads/{quoted}",
                                 {"text": text, "role": role})
        return body if isinstance(body, dict) else {}

    def list_sessions(self, cwd: str, kind: str | None = None) -> list[dict]:
        query = f"?cwd={urllib.parse.quote(cwd, safe='')}"
        if kind:
            query += f"&kind={urllib.parse.quote(kind, safe='')}"
        _, body = self._request("GET", f"/api/sessions{query}")
        return body if isinstance(body, list) else []

    def register_assets(self, sid: str, static_root: str, entry: str | None = None) -> None:
        self._request("POST", f"/s/{sid}/api/assets",
                       {"static_root": static_root, "entry": entry})

    def finish(self, sid: str) -> None:
        self._request("POST", f"/s/{sid}/api/finish")

    def cancel(self, sid: str) -> None:
        self._request("POST", f"/s/{sid}/api/cancel")

    def forget(self, sid: str, force: bool = False, kind: str = "") -> dict:
        """Delete a session and its whole workspace. Irreversible.

        `kind` disambiguates a slug that exists under more than one kind; the
        daemon answers 409 naming the candidates rather than picking one."""
        from urllib.parse import quote, urlencode
        query = {}
        if kind:
            query["kind"] = kind
        if force:
            query["force"] = "1"
        suffix = ("?" + urlencode(query)) if query else ""
        return self._request("DELETE", f"/s/{quote(sid, safe='')}/{suffix}")

    def unfinish(self, sid: str) -> dict:
        _, body = self._request("POST", f"/s/{sid}/api/unfinish")
        return body if isinstance(body, dict) else {}
