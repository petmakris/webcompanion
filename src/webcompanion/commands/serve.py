"""`webcompanion serve` -- run the daemon in the foreground until signalled.

This is what launchd/systemd actually exec (`env python3 webcompanion.pyz
serve`). Before binding, it performs a startup self-check: `ThreadingHTTPServer`
sets `SO_REUSEADDR`, so binding straight over a stale process would silently
split requests between two servers at random rather than fail loudly -- and
a supervisor that restarts on crash makes that reachable. Refusing to start,
and naming the holder, is the single most useful line the old per-skill
launchers printed.
"""
from __future__ import annotations

import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

from webcompanion import config as cfgmod
from webcompanion import paths
from webcompanion.server import Daemon


def port_holder(port: int) -> tuple[str, int] | None:
    """Best-effort (process name, pid) holding `port`, or None if free or
    undeterminable. Shells out to `lsof`, available on macOS and Linux both,
    rather than adding a dependency for something the old launcher already
    got right by doing the same thing."""
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        out = subprocess.run(
            [lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [ln for ln in out.splitlines() if ln and not ln.startswith("COMMAND")]
    if not lines:
        return None
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return None


def _can_ask_who_holds_the_port() -> bool:
    """Whether `port_holder` is able to answer at all. It returns None both for
    "nobody is listening" and for "lsof is not installed", and those two lead to
    opposite decisions."""
    return shutil.which("lsof") is not None


def _port_is_free(bind: str, port: int) -> bool:
    """A plain bind with no SO_REUSEADDR: the one check that actually
    detects a listener already on this port, which `Daemon.start()` (via
    `ThreadingHTTPServer`'s `SO_REUSEADDR`) would not.

    The family comes from `bind` itself. Hardcoding AF_INET meant a
    configured `bind = "::1"` could never bind here, so this check refused
    the port permanently and the daemon never started -- on an address
    gate.py went to real trouble to support.
    """
    try:
        infos = socket.getaddrinfo(bind, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False  # an address that does not resolve is not one we can use
    for family, socktype, proto, _canon, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        try:
            s.bind(sockaddr)
        except OSError:
            return False
        finally:
            s.close()
    return True


def _pidfile_path() -> Path:
    return paths.state_root() / "webcompanion.pid"


def _write_pidfile() -> None:
    import os
    p = _pidfile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(os.getpid()))


def _remove_pidfile() -> None:
    try:
        _pidfile_path().unlink()
    except OSError:
        pass


def run(argv: list[str], *, _stop_event: threading.Event | None = None) -> int:
    cfg = cfgmod.load()

    # A failed plain bind is EVIDENCE, not a verdict. It fails for a listening
    # process -- what this guard is for -- and equally for a socket merely still
    # in the kernel's table: a half-closed connection a client has not let go of.
    #
    # That second case is not hypothetical here. Every open page holds an SSE
    # connection, and a browser that outlives the daemon leaves those sockets in
    # FIN_WAIT_2 with nothing listening behind them. Refusing then turns an open
    # tab into an outage: launchd's KeepAlive restarts the daemon, the residue is
    # still there, and it crashloops until the browser happens to let go.
    #
    # So the refusal is owned by `port_holder`, which asks lsof for a LISTENer and
    # answers about a process rather than about a port. No listener means nothing
    # to be stale alongside, and SO_REUSEADDR binds straight over the residue.
    if not _port_is_free(cfg.bind, cfg.port):
        holder = port_holder(cfg.port)
        if holder is not None:
            name, pid = holder
            print(f"webcompanion: port {cfg.port} is already held by "
                  f"{name} (pid {pid}); refusing to bind alongside a stale "
                  f"process.", file=sys.stderr)
            print(f"  change it with the \"port\" field in "
                  f"{cfgmod.config_path()} (see the README's Configuration "
                  f"section), then restart the service.", file=sys.stderr)
            return 1
        if not _can_ask_who_holds_the_port():
            # Without lsof there is no way to tell a listener from residue, and
            # binding alongside a real one would split traffic silently. Keep the
            # conservative refusal for the only case that cannot be distinguished.
            print(f"webcompanion: port {cfg.port} is already in use and lsof is "
                  f"not available to say by what; refusing to bind alongside a "
                  f"possible stale process.", file=sys.stderr)
            print(f"  change it with the \"port\" field in "
                  f"{cfgmod.config_path()} (see the README's Configuration "
                  f"section), then restart the service.", file=sys.stderr)
            return 1
        print(f"webcompanion: port {cfg.port} still holds closed connections "
              f"(no process is listening) -- binding over them.", file=sys.stderr)

    daemon = Daemon(cfg)
    daemon.start()
    _write_pidfile()

    stop_event = _stop_event if _stop_event is not None else threading.Event()

    def _handle(signum, frame):
        stop_event.set()

    old_term = signal.signal(signal.SIGTERM, _handle)
    old_int = signal.signal(signal.SIGINT, _handle)
    try:
        stop_event.wait()
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        daemon.stop()
        _remove_pidfile()
    return 0
