"""`webcompanion doctor` -- succeeds the older `annotate-doctor`.

Reports, in order: python3 and its version, the config file and its mode,
the zipapp's presence, the interpreter the installed service actually
references (a dangling one is the Homebrew-retired-the-venv failure this
whole task exists to end), the port holder, the restart count (a respawn
loop), and the health response. Never installs anything -- see
`commands/_common.py`.
"""
from __future__ import annotations

import json
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from webcompanion import config as cfgmod
from webcompanion import paths
from webcompanion.client import Client, ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands.install_service import (
    DEFAULT_LABEL,
    DEFAULT_SERVICE_NAME,
    default_plist_path,
    default_unit_path,
    default_zipapp_path,
)
from webcompanion.commands.serve import port_holder as _port_holder

REPORTABLE = (DaemonUnreachable, ContractMismatch, HttpError)

# More than this many recent restarts looks like ThrottleInterval firing in
# a loop rather than a one-off crash.
RESTART_LOOP_THRESHOLD = 5

SWEEP_FAILURE_MARKER = "startup_sweep_failed.json"


def _service_program_args() -> list[str] | None:
    """The literal argv the installed plist/unit execs, or None if neither
    is installed."""
    plist = default_plist_path(DEFAULT_LABEL)
    if plist.is_file():
        try:
            import plistlib
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            return None
        args = data.get("ProgramArguments")
        return list(args) if isinstance(args, list) else None

    unit = default_unit_path(DEFAULT_SERVICE_NAME)
    if unit.is_file():
        try:
            text = unit.read_text()
        except OSError:
            return None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("ExecStart="):
                return shlex.split(line.split("=", 1)[1])
    return None


def _service_interpreter() -> Path | None:
    """The interpreter the installed service actually references, or None
    if no service is installed, or if it is installed the safe way (via
    `/usr/bin/env`, which never dangles -- see install_service.py). An old
    pipx-venv install instead named an absolute interpreter path directly;
    that path is what can go dangling and is what this checks for."""
    args = _service_program_args()
    if not args:
        return None
    first = args[0]
    if first == "/usr/bin/env":
        return None
    return Path(first)


def _recent_restart_count() -> int:
    """Best-effort restart count for the installed service. systemd exposes
    this directly (`NRestarts`); macOS's `launchctl` does not expose an
    equivalent without the service actually being installed and inspected
    live, so this reports 0 on macOS -- see the task report for what is and
    is not verifiable without installing a real service."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return 0
    try:
        out = subprocess.run(
            [systemctl, "--user", "show", DEFAULT_SERVICE_NAME, "-p", "NRestarts"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.splitlines():
        if line.startswith("NRestarts="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return 0
    return 0


def _health() -> dict | None:
    cfg = cfgmod.load()
    client = Client(f"http://{cfg.bind}:{cfg.port}", cfg.token)
    try:
        return client.health()
    except REPORTABLE:
        return None


def _sweep_failure_marker() -> dict | None:
    p = paths.state_root() / SWEEP_FAILURE_MARKER
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run(argv: list[str]) -> int:
    ok = True

    print(f"python3: {sys.executable} ({platform.python_version()})")

    cfg_path = cfgmod.config_path()
    if not cfg_path.exists():
        ok = False
        print(f"config: missing at {cfg_path}")
        print("  run: webcompanion install-service")
    else:
        mode = cfg_path.stat().st_mode & 0o777
        print(f"config: {cfg_path} (mode {oct(mode)})")
        if mode != 0o600:
            print(f"  warning: expected mode 0600, got {oct(mode)} -- "
                  f"the write token may be readable by other users")

    pyz = default_zipapp_path()
    print(f"zipapp: {pyz} ({'present' if pyz.is_file() else 'missing'})")

    interp = _service_interpreter()
    if interp is None:
        print("service interpreter: no service installed, or resolved via "
              "/usr/bin/env (never dangles)")
    elif not interp.exists():
        ok = False
        print(f"service interpreter: {interp} is DANGLING (no longer "
              f"exists) -- the service will respawn-loop; reinstall with "
              f"`webcompanion install-service`")
    else:
        print(f"service interpreter: {interp} (present)")

    restarts = _recent_restart_count()
    print(f"recent restarts: {restarts}")
    if restarts >= RESTART_LOOP_THRESHOLD:
        ok = False
        print(f"  warning: {restarts} restarts recently -- looks like a "
              f"respawn loop; check the log")

    health = _health()
    if health is None:
        ok = False
        cfg = cfgmod.load()
        holder = _port_holder(cfg.port)
        if holder is not None:
            name, pid = holder
            print(f"health: unreachable -- port {cfg.port} held by "
                  f"{name} (pid {pid})")
        else:
            print(f"health: unreachable -- port {cfg.port} appears free; "
                  f"the service does not seem to be running")
    else:
        print(f"health: ok -- {health.get('banner', '')}, "
              f"contract {health.get('contract')}, "
              f"sessions {health.get('sessions', 0)}")

    failure = _sweep_failure_marker()
    if failure is not None:
        ok = False
        marker_path = paths.state_root() / SWEEP_FAILURE_MARKER
        print(f"startup cleanup: FAILED at {failure.get('when')} -- "
              f"{failure.get('error')}")
        print(f"  the daemon still started, but its startup cleanup sweep "
              f"did not run; see {marker_path}")

    return 0 if ok else 1
