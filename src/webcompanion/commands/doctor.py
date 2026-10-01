"""`webcompanion doctor` -- succeeds the older `annotate-doctor`.

Reports, in order: python3 and its version, the config file and its mode,
whether `sessions.json` parses, the zipapp's presence, whether the service
is installed AT ALL and whether launchd/systemd knows the job, the
interpreter the installed service actually references (a dangling one is the
Homebrew-retired-the-venv failure this whole task exists to end), the port
holder, the restart count (a respawn loop), the health response, the last
lines of the service log, and any startup-sweep failure or refusal. Never
installs anything -- see `commands/_common.py`.
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
from webcompanion import speech
from webcompanion.client import Client, ContractMismatch, DaemonUnreachable, HttpError
from webcompanion.commands.install_service import (
    DEFAULT_LABEL,
    DEFAULT_SERVICE_NAME,
    default_log_path,
    default_plist_path,
    default_unit_path,
    default_zipapp_path,
)
from webcompanion.commands.serve import port_holder as _port_holder
from webcompanion.registry import ABSENT, LOADED, UNREADABLE, Registry

REPORTABLE = (DaemonUnreachable, ContractMismatch, HttpError)

# More than this many recent restarts looks like ThrottleInterval firing in
# a loop rather than a one-off crash.
RESTART_LOOP_THRESHOLD = 5

SWEEP_FAILURE_MARKER = "startup_sweep_failed.json"

# How much of the service log to show. Enough to carry a traceback and the
# lines around it; not so much that the report stops being readable.
LOG_TAIL_LINES = 20


def _service_file() -> tuple[Path, bool]:
    """(the path install-service would write, whether it is there).

    "No service installed" and "service installed correctly" used to print
    the SAME line -- "no service installed, or resolved via /usr/bin/env" --
    so `doctor` could not tell a machine that never ran install-service from
    a healthy one. They are opposite diagnoses with opposite fixes.
    """
    path = (default_plist_path(DEFAULT_LABEL) if sys.platform == "darwin"
            else default_unit_path(DEFAULT_SERVICE_NAME))
    return path, path.is_file()


def _supervisor_knows_the_job() -> bool | None:
    """Whether launchd/systemd has the job loaded, or None if unknowable.

    A plist on disk is not a running service: launchd only knows about it
    once it has been bootstrapped, and an install that wrote the file but
    failed to load it looks identical on disk to one that worked.
    """
    if sys.platform == "darwin":
        launchctl = shutil.which("launchctl")
        if not launchctl:
            return None
        try:
            proc = subprocess.run([launchctl, "list", DEFAULT_LABEL],
                                  capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.returncode == 0
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        proc = subprocess.run(
            [systemctl, "--user", "is-enabled", DEFAULT_SERVICE_NAME],
            capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode == 0


def log_path() -> Path:
    """Where install-service points the service's stderr.

    Derived from `install_service.service_log_paths`, the same function that
    renders the plist and the unit, so the reader and the writer cannot
    disagree about the filename again.
    """
    return default_log_path()


def _log_tail(n: int = LOG_TAIL_LINES) -> list[str] | None:
    """The last `n` lines of the service log, or None if there is no log.

    A user told "see the log" and left to find it has been told nothing --
    launchd rotates it, and nobody reads it until something else breaks.
    """
    try:
        text = log_path().read_text(errors="replace")
    except OSError:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return lines[-n:]


def _sessions_file_status() -> tuple[str, int]:
    """(status, row count) for sessions.json -- the file whose loss deletes
    data, because the stray sweep removes every workspace no row points at.

    This is the same check Registry.rehydrate makes on boot, run here so a
    user can see the problem BEFORE the next restart rather than after it.
    """
    reg = Registry(paths.state_root())
    status = reg.rehydrate()
    return status, len(reg.items())


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


def _recent_restart_count() -> int | None:
    """Restart count for the installed service, or None when it genuinely
    cannot be known. systemd exposes this directly (`NRestarts`); macOS's
    `launchctl` has no equivalent without the service being installed and
    inspected live -- see the task report for what is and is not verifiable
    without installing a real service. None must never be reported as 0: a
    fabricated zero reads as evidence of health to an operator debugging a
    respawn loop."""
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return None
    try:
        out = subprocess.run(
            [systemctl, "--user", "show", DEFAULT_SERVICE_NAME, "-p", "NRestarts"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("NRestarts="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


# The minimum Python this package supports (see pyproject.toml).
MIN_PYTHON = (3, 9)

# launchd's actual default job environment -- NOT the interactive shell's
# PATH, which is exactly the point: a Homebrew python3 on Apple Silicon
# lives in /opt/homebrew/bin, reachable from an interactive shell via
# `brew shellenv` but NOT from this minimal launchd/systemd PATH, so
# `/usr/bin/env python3` inside the actual service can resolve to a
# completely different, possibly older, interpreter than the one running
# doctor. Resolving it for real -- not guessing -- is what catches that
# drift before it becomes a respawn loop with nothing saying why.
_SERVICE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _resolved_service_python() -> tuple[Path, tuple[int, ...]] | None:
    """What `/usr/bin/env python3` actually resolves to under launchd's
    minimal PATH -- not `sys.executable`, which is whatever interpreter
    doctor itself happens to be running on. Returns (path, version) or None
    if no python3 can be found there at all."""
    env_bin = shutil.which("env") or "/usr/bin/env"
    try:
        proc = subprocess.run(
            [env_bin, "python3", "-c",
             "import sys; print(sys.executable); "
             "print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=5,
            env={"PATH": _SERVICE_PATH},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = proc.stdout.strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        version = tuple(int(x) for x in lines[1].split("."))
    except ValueError:
        return None
    return Path(lines[0]), version


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

    print(f"python3 (running doctor): {sys.executable} ({platform.python_version()})")

    resolved = _resolved_service_python()
    if resolved is None:
        ok = False
        print(f"python3 (via /usr/bin/env, the service's minimal PATH): "
              f"NOT FOUND -- the service would fail to exec")
    else:
        svc_path, svc_version = resolved
        svc_version_str = ".".join(map(str, svc_version))
        if svc_version[:2] < MIN_PYTHON:
            ok = False
            min_str = ".".join(map(str, MIN_PYTHON))
            print(f"python3 (via /usr/bin/env, the service's minimal PATH): "
                  f"{svc_path} ({svc_version_str}) -- BELOW the required "
                  f"{min_str}; the service will fail to exec")
        else:
            print(f"python3 (via /usr/bin/env, the service's minimal PATH): "
                  f"{svc_path} ({svc_version_str})")

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

    sessions_status, rows = _sessions_file_status()
    sessions_file = paths.state_root() / "sessions.json"
    if sessions_status == LOADED:
        print(f"sessions.json: parses, {rows} live session(s)")
    elif sessions_status == ABSENT:
        print(f"sessions.json: not written yet at {sessions_file} "
              f"(normal before the first session)")
    else:
        ok = False
        print(f"sessions.json: UNREADABLE at {sessions_file}")
        print(f"  the daemon cannot tell which workspaces are live, so it "
              f"refuses its startup stray sweep rather than deleting them.")
        print(f"  fix or remove that file, then restart the service.")

    pyz = default_zipapp_path()
    print(f"zipapp: {pyz} ({'present' if pyz.is_file() else 'missing'})")

    service_path, service_installed = _service_file()
    if not service_installed:
        ok = False
        print(f"service: NOT INSTALLED -- nothing at {service_path}")
        print("  run: webcompanion install-service")
    else:
        known = _supervisor_knows_the_job()
        supervisor = "launchd" if sys.platform == "darwin" else "systemd"
        if known is True:
            print(f"service: installed at {service_path}, and {supervisor} "
                  f"knows the job")
        elif known is False:
            ok = False
            print(f"service: installed at {service_path}, but {supervisor} "
                  f"does NOT know the job -- the file was written and never "
                  f"loaded")
            print("  run: webcompanion install-service")
        else:
            print(f"service: installed at {service_path} ({supervisor} could "
                  f"not be queried)")

    interp = _service_interpreter()
    if interp is None:
        print("service interpreter: resolved via /usr/bin/env (never dangles)"
              if service_installed else
              "service interpreter: n/a -- no service installed")
    elif not interp.exists():
        ok = False
        print(f"service interpreter: {interp} is DANGLING (no longer "
              f"exists) -- the service will respawn-loop; reinstall with "
              f"`webcompanion install-service`")
    else:
        print(f"service interpreter: {interp} (present)")

    restarts = _recent_restart_count()
    if restarts is None:
        print("recent restarts: unknown (launchctl does not expose a "
              "restart count)")
    else:
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
        if failure.get("refused"):
            print(f"startup cleanup: the stray sweep was REFUSED at "
                  f"{failure.get('when')} -- {failure['refused']}")
            print(f"  nothing was deleted, which is the point. Fix the cause "
                  f"and restart; see {marker_path}")
        elif failure.get("error"):
            print(f"startup cleanup: FAILED at {failure.get('when')} -- "
                  f"{failure.get('error')}")
            print(f"  the daemon still started, but its startup cleanup sweep "
                  f"did not run; see {marker_path}")
        preserved = failure.get("preserved_unreadable_registry")
        if preserved:
            print(f"registry: sessions.json could not be parsed and was "
                  f"preserved as {preserved}")
            print(f"  nothing was overwritten. Salvage it or delete it, then "
                  f"restart; see {marker_path}")

    # Speech is optional: a machine without it still serves every page, so
    # nothing here can fail the doctor. It prints only whether the key is
    # set, never the key.
    env_path = paths.state_root() / speech.ENV_FILE
    if not env_path.exists():
        print("speech: not set up (optional) -- read-aloud and dictation need "
              f"AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in {env_path}")
    else:
        scfg = speech.load(paths.state_root())
        st = speech.status(scfg)
        key_state = "key set" if scfg.key else "key MISSING"
        print(f"speech: {key_state}, region {scfg.region or 'MISSING'}, "
              f"claude {speech.resolve_claude(scfg) or 'NOT FOUND'}")
        mode = env_path.stat().st_mode & 0o777
        if mode != 0o600:
            print(f"  warning: {env_path} has mode {oct(mode)}, expected mode 0600 "
                  f"-- the Azure key may be readable by other users")
        if st.get("reason"):
            print(f"  {st['reason']}")

    tail = _log_tail()
    if tail is None:
        print(f"log: nothing at {log_path()} yet")
    elif not tail:
        print(f"log: {log_path()} is empty (nothing has gone wrong)")
    else:
        print(f"log: last {len(tail)} line(s) of {log_path()}")
        for line in tail:
            print(f"  | {line}")

    return 0 if ok else 1
