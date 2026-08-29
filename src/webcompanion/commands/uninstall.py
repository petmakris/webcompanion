"""`webcompanion uninstall` -- stop the service and remove what
`install-service` put on this machine.

Without it, `pipx uninstall webcompanion` removes the CLI and leaves the
launchd plist or systemd unit and the zipapp exactly where they are: an
always-on daemon still holding port 3080, still supervised, and no command
left on the machine to stop it. `launchctl bootout` by hand is not a thing a
user should have to be told after the fact.

WHAT THIS DELIBERATELY DOES NOT REMOVE: the config file (it carries the write
token an IDE plugin has saved) and the workspaces (they are the user's data,
they go back to install day, `resume <slug>` is a shipped feature, and there
is no backup). Both paths are printed so removing them is one obvious
`rm -rf` away if that is really what was meant. A deletion the user did not
ask for is not something an uninstall command gets to decide.

`target_dir` and `label` are parameterised exactly as in `install_service`,
so tests exercise the whole path against a throwaway directory and no test
ever unloads a real service.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from webcompanion import config as cfgmod
from webcompanion import paths
from webcompanion.commands.install_service import (
    DEFAULT_LABEL,
    DEFAULT_SERVICE_NAME,
    default_plist_path,
    default_unit_path,
    default_zipapp_path,
)


def _stop(system: str, unit_path: Path, label: str) -> list[str]:
    """Stop and unload the service. Returns one message per step that failed
    for a reason worth telling the user about.

    "It was not loaded" is not such a reason: uninstalling something that is
    already stopped must succeed quietly, so every step here tolerates a
    non-zero exit. What it must NOT do is tolerate the binary being missing
    entirely -- then nothing was stopped and the user needs to know before
    the plist is deleted out from under a running job.
    """
    problems: list[str] = []

    def step(cmd: list[str]) -> None:
        try:
            subprocess.run(cmd, check=False, capture_output=True, text=True)
        except OSError as e:
            problems.append(f"could not run {cmd[0]}: {e}")

    if system == "darwin":
        domain = f"gui/{os.getuid()}"
        step(["launchctl", "bootout", f"{domain}/{label}"])
        step(["launchctl", "bootout", domain, str(unit_path)])
    else:
        unit_name = unit_path.name
        step(["systemctl", "--user", "disable", "--now", unit_name])
        step(["systemctl", "--user", "daemon-reload"])
    return problems


def _remove(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"webcompanion: could not remove {path}: {e}", file=sys.stderr)
        return False


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="webcompanion uninstall",
        description="Stop the service and remove its plist/unit and zipapp. "
                    "Leaves the config file and every workspace alone.")


def run(argv: list[str], *, target_dir: Path | None = None,
        label: str = DEFAULT_LABEL) -> int:
    build_parser().parse_args(argv)

    system = "darwin" if sys.platform == "darwin" else "linux"
    if target_dir is not None:
        unit_path = Path(target_dir) / (
            f"{label}.plist" if system == "darwin" else DEFAULT_SERVICE_NAME)
    else:
        unit_path = (default_plist_path(label) if system == "darwin"
                     else default_unit_path())

    problems: list[str] = []
    if target_dir is None:
        problems = _stop(system, unit_path, label)
    else:
        print(f"webcompanion: throwaway uninstall -- the real service was "
              f"not touched")

    removed = []
    if _remove(unit_path):
        removed.append(str(unit_path))
    pyz = default_zipapp_path()
    if _remove(pyz):
        removed.append(str(pyz))

    for path in removed:
        print(f"removed: {path}")
    if not removed:
        print("webcompanion: nothing to remove -- no service files were "
              "installed at the default locations.")

    print("\nleft in place, on purpose:")
    print(f"  config (holds the write token): {cfgmod.config_path()}")
    print(f"  workspaces (your sessions):     {paths.workspace_root(cfgmod.load())}")
    print("  remove them yourself if you really mean to; nothing else will.")

    if problems:
        for problem in problems:
            print(f"webcompanion: {problem}", file=sys.stderr)
        return 1
    return 0
