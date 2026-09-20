"""Who may write, and whether the two sides agree on the contract.

This is the only module that answers either question. Everything else asks it.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

from webcompanion import CONTRACT

WRITE_TOKEN_HEADER = "X-WebCompanion-Token"
CONTRACT_HEADER = "X-WebCompanion-Contract"

# Tailscale's fixed CGNAT range (100.64.0.0/10) and its per-tailnet IPv6 ULA
# prefix (fd7a:115c:a1e0::/48 -- constant across every tailnet, not just this
# one). Used only to decide whether an address is worth a `tailscale whois`
# call at all; being in range is not itself a trust decision.
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_WHOIS_TIMEOUT = 1.5
_WHOIS_CACHE_SECONDS = 30.0
_whois_cache: dict[str, tuple[float, str | None]] = {}
_whois_cache_lock = threading.Lock()

# Where Tailscale actually installs, checked when `PATH` doesn't have it. A
# launchd LaunchAgent's default environment is `PATH=/usr/bin:/bin:/usr/sbin:
# /sbin` (verified against this daemon's own service) -- it never sees an
# interactive shell's PATH, so a bare `["tailscale", ...]` fails with
# FileNotFoundError every time the daemon actually runs it, silently, since
# _tailscale_login's blanket `except` turns that into an ordinary refusal
# with no other sign anything is wrong.
#
# Deliberately EXCLUDES the macOS App Store build's own binary
# (/Applications/Tailscale.app/Contents/MacOS/Tailscale): verified by direct
# invocation that it aborts ("The Tailscale GUI failed to start ... CLIError
# error 3") unless argv[0] itself looks like a path inside the bundle -- a
# wrapper script can `exec` it with the right argv[0], but a second,
# different absolute path to the same binary here would not, so listing it
# would silently prefer a candidate that never works.
_TAILSCALE_BIN_CANDIDATES = (
    os.path.expanduser("~/.local/bin/tailscale"),
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/bin/tailscale",
    "/usr/sbin/tailscale",
)
_tailscale_bin_lock = threading.Lock()
_tailscale_bin_resolved = False
_tailscale_bin_path: str | None = None


def _tailscale_bin() -> str | None:
    """The `tailscale` binary's absolute path, resolved once and cached.

    Tries `PATH` first (respects an environment where it IS set up), then
    falls back to the handful of places the macOS package, Homebrew, and a
    user-local install actually put it. None means none of those panned out.
    """
    global _tailscale_bin_resolved, _tailscale_bin_path
    with _tailscale_bin_lock:
        if _tailscale_bin_resolved:
            return _tailscale_bin_path
        found = shutil.which("tailscale")
        if not found:
            for candidate in _TAILSCALE_BIN_CANDIDATES:
                if os.access(candidate, os.X_OK):
                    found = candidate
                    break
        _tailscale_bin_path = found
        _tailscale_bin_resolved = True
        return found


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_tailscale_addr(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip in _TAILSCALE_V4 or ip in _TAILSCALE_V6


def _tailscale_login(addr: str, run=subprocess.run, tailscale_bin: str | None = "") -> str | None:
    """The tailnet login that owns `addr`, or None if it isn't one of ours.

    Shells out to `tailscale whois` rather than talking to tailscaled's
    LocalAPI directly -- the CLI is the documented, stable surface for this
    question, and it is cheap enough next to a write request that a short
    per-address cache is the only optimisation this needs. A missing/hung
    `tailscale` binary, a non-tailnet address, or any parse failure all fail
    CLOSED (None) rather than raise -- this sits on the request path that
    decides who may write, and a bug here must never turn into a 500 or a
    hang, only a refusal.

    `tailscale_bin=""` (the default) means "resolve it via `_tailscale_bin()`"
    -- a caller can pass an explicit path (or None) to bypass that lookup,
    which is what the tests do so they exercise the run/parse logic without
    depending on where THIS machine happens to keep the binary.
    """
    now = time.monotonic()
    with _whois_cache_lock:
        cached = _whois_cache.get(addr)
        if cached is not None and now - cached[0] < _WHOIS_CACHE_SECONDS:
            return cached[1]
    login: str | None = None
    binary = _tailscale_bin() if tailscale_bin == "" else tailscale_bin
    try:
        if binary is None:
            raise FileNotFoundError("tailscale binary not found")
        out = run([binary, "whois", "--json", addr],
                  capture_output=True, text=True, timeout=_WHOIS_TIMEOUT, check=True)
        info = json.loads(out.stdout)
        # `MachineAuthorized` only ever appears on the daemon's OWN node
        # (verified against a live peer: a phone actually connected over the
        # tailnet has no such key in its `whois` output at all). Reaching us
        # as a routable peer already proves the tailnet authorized it, so an
        # explicit `false` is the only signal worth refusing on; absent means
        # trust the login name.
        if info.get("Node", {}).get("MachineAuthorized") is not False:
            login = info.get("UserProfile", {}).get("LoginName") or None
    except Exception:
        login = None
    with _whois_cache_lock:
        _whois_cache[addr] = (now, login)
    return login


def _header(handler, name: str) -> str:
    try:
        return (handler.headers.get(name) or "").strip()
    except AttributeError:
        return ""


class _Unparseable:
    """Sentinel returned for a header `urlsplit` refuses to parse.

    Never equal to anything — including itself as a fresh instance, and
    including `None` — so a parse failure on either side of the Origin/Host
    comparison in `is_owner` can never accidentally compare equal and must
    always fall through to a refusal.
    """

    def __eq__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return id(self)


_UNPARSEABLE = _Unparseable()


def _hostname(url: str) -> str | object | None:
    """`urlsplit(url).hostname`, but a malformed URL fails closed.

    `urlsplit` raises `ValueError: Invalid IPv6 URL` on unbalanced brackets
    (e.g. `Origin: http://[` or `Host: [`). Both headers are fully
    attacker-controlled on a raw request, so an unhandled raise here would
    surface as a 500 from the one function that decides whether a request
    may write — the worst shape a bug in this module can take. A header we
    cannot parse is a header we do not trust, so this returns a sentinel
    that never compares equal to anything, never `None`, never itself.
    """
    try:
        return urlsplit(url).hostname
    except ValueError:
        return _UNPARSEABLE


def _host_hostname(host: str) -> str | object | None:
    """Extract the hostname from a Host header, IPv6-literal-safe.

    `urlsplit` only parses `[::1]:3080` correctly when it looks like a URL,
    so prefix `//` to get netloc parsing rather than hand-rolling a
    bracket-strip. A bare `rsplit(":", 1)` on `[::1]:3080` yields `[::1]`,
    which never equals `urlsplit(origin).hostname`'s `::1` — that mismatch
    would refuse the owner on IPv6 loopback.
    """
    return _hostname(f"//{host}") if host else None


def is_owner(handler, token: str, owner_login: str | None = None) -> bool:
    """Three ways to be the owner, and no fourth.

    Loopback is the owner by construction — nobody else can reach it, so the
    CLI and the browser on this machine both pass without configuration.
    Everyone else needs either the capability token (handed out only through
    the owner URL) or, when `owner_login` is configured, a Tailscale identity
    that resolves to it: a caller reaching us from an authorized node on our
    own tailnet, logged in as us. The second path is opt-in (`owner_login`
    defaults to None, i.e. off) because it shells out per uncached address and
    depends on the `tailscale` binary being present and tailscaled running.

    But loopback alone is not enough, because JavaScript on ANY website runs
    from loopback: a page you merely visit could otherwise reach this daemon
    as the owner. `Content-Type: text/plain` makes a POST a CORS "simple
    request", which the browser sends with no preflight to stop it, and the
    handlers json.loads the body without consulting Content-Type. That is a
    working delete-everything gadget for any site you open.

    `Sec-Fetch-Site` is the reliable signal — the browser sets it and script
    cannot forge it. Non-browser callers send neither it nor Origin and are
    unaffected; requiring one would break them.
    """
    site = _header(handler, "Sec-Fetch-Site").lower()
    if site and site not in ("same-origin", "same-site", "none"):
        return False

    origin = _header(handler, "Origin")
    if origin:
        # HOST ONLY, deliberately: the owner may reach the daemon over either
        # scheme and through a port-forward, and neither changes who they are.
        if _hostname(origin) != _host_hostname(_header(handler, "Host")):
            return False

    if is_loopback(handler.client_address[0]):
        return True

    addr = handler.client_address[0]
    presented = _header(handler, WRITE_TOKEN_HEADER)
    if presented and token and secrets.compare_digest(presented, token):
        return True

    if owner_login and _is_tailscale_addr(addr):
        return _tailscale_login(addr) == owner_login

    return False


def check_contract(handler) -> tuple[bool, str]:
    """(ok, message). A missing header is tolerated — a hand-run curl or a
    health probe is not a contract violation. A header that disagrees is not
    tolerated, because the four artifacts speaking this contract update on
    different schedules and a silent mismatch presents as a dead poll."""
    raw = _header(handler, CONTRACT_HEADER)
    if not raw:
        return True, ""
    try:
        sent = int(raw)
    except ValueError:
        return False, (f"unreadable {CONTRACT_HEADER}: {raw!r}; "
                       f"this daemon speaks contract {CONTRACT}")
    if sent == CONTRACT:
        return True, ""
    if sent < CONTRACT:
        return False, (f"the client speaks contract {sent}, this daemon speaks "
                       f"{CONTRACT}; update the client")
    return False, (f"the client speaks contract {sent}, this daemon speaks "
                   f"{CONTRACT}; update the daemon with "
                   f"`pipx upgrade webcompanion && webcompanion install-service`")
