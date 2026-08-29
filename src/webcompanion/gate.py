"""Who may write, and whether the two sides agree on the contract.

This is the only module that answers either question. Everything else asks it.
"""
from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from webcompanion import CONTRACT

WRITE_TOKEN_HEADER = "X-WebCompanion-Token"
CONTRACT_HEADER = "X-WebCompanion-Contract"


def is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


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


def is_owner(handler, token: str) -> bool:
    """Two ways to be the owner, and no third.

    Loopback is the owner by construction — nobody else can reach it, so the
    CLI and the browser on this machine both pass without configuration.
    Everyone else needs the capability token, handed out only through the
    owner URL.

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

    presented = _header(handler, WRITE_TOKEN_HEADER)
    if not presented or not token:
        return False
    return secrets.compare_digest(presented, token)


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
