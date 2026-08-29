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

    if not _port_is_free(cfg.bind, cfg.port):
        holder = port_holder(cfg.port)
        if holder is not None:
            name, pid = holder
            print(f"webcompanion: port {cfg.port} is already held by "
                  f"{name} (pid {pid}); refusing to bind alongside a stale "
                  f"process.", file=sys.stderr)
        else:
            print(f"webcompanion: port {cfg.port} is already in use "
                  f"(holder could not be determined); refusing to bind "
                  f"alongside a stale process.", file=sys.stderr)
        print(f"  change it with the \"port\" field in "
              f"{cfgmod.config_path()} (see the README's Configuration "
              f"section), then restart the service.", file=sys.stderr)
        return 1

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
