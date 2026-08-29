"""`webcompanion install-service` -- build the zipapp, write the launchd
plist or systemd unit, and (re)start the service.

Why a zipapp and not a pipx venv: a venv is bound to the interpreter that
created it. When Homebrew retires that Python, the venv's entry point fails
to exec, and launchd's KeepAlive respawn-loops the job forever at the
default 10s ThrottleInterval -- `launchctl list` shows the job, every skill
sees connection refused, and nothing says why. The package is stdlib-only,
so a venv buys nothing and costs exactly that failure. The plist instead
runs `/usr/bin/env python3 <path>/webcompanion.pyz serve`: `python3` is
resolved fresh from PATH on every launch, so replacing the system Python
never leaves a dangling interpreter path baked into the service definition.

`ensure_config` mints a token only when there is none -- reminting on every
install would invalidate the IntelliJ plugin's credential mid-session, and
this command is meant to be re-run on every upgrade.
"""
from __future__ import annotations

import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import zipapp
from importlib.resources import as_file, files
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from webcompanion import config as cfgmod
from webcompanion import paths
from webcompanion.config import Config

DEFAULT_LABEL = "dev.webcompanion"
DEFAULT_SERVICE_NAME = "webcompanion.service"
DEFAULT_THROTTLE_SECONDS = 10
DEFAULT_RESTART_SECONDS = 10


def default_zipapp_path() -> Path:
    return Path("~/.local/share/webcompanion/webcompanion.pyz").expanduser()


def default_plist_path(label: str = DEFAULT_LABEL) -> Path:
    return Path(f"~/Library/LaunchAgents/{label}.plist").expanduser()


def default_unit_path(name: str = DEFAULT_SERVICE_NAME) -> Path:
    return Path(f"~/.config/systemd/user/{name}").expanduser()


def _read_template(name: str) -> str:
    with as_file(files("webcompanion").joinpath("service", name)) as p:
        return p.read_text()


def render_plist(*, pyz: Path, log_dir: Path, label: str,
                  throttle: int = DEFAULT_THROTTLE_SECONDS) -> str:
    log_dir = Path(log_dir)
    tmpl = string.Template(_read_template("dev.webcompanion.plist"))
    return tmpl.substitute(
        label=_xml_escape(label),
        pyz=_xml_escape(str(pyz)),
        throttle=str(int(throttle)),
        out_log=_xml_escape(str(log_dir / f"{label}.out.log")),
        err_log=_xml_escape(str(log_dir / f"{label}.log")),
    )


def render_unit(*, pyz: Path, log_dir: Path | None = None,
                 restart_sec: int = DEFAULT_RESTART_SECONDS) -> str:
    log_dir = Path(log_dir) if log_dir is not None else paths.state_root()
    tmpl = string.Template(_read_template("webcompanion.service"))
    return tmpl.substitute(
        pyz=str(pyz),
        restart_sec=str(int(restart_sec)),
        out_log=str(log_dir / "webcompanion.out.log"),
        err_log=str(log_dir / "webcompanion.log"),
    )


def build_zipapp(dest: Path) -> Path:
    """Stage a copy of the installed package and zip it with `zipapp`.

    Reached via `importlib.resources.as_file`, not `Path(__file__).parent`,
    so this keeps working if the package is ever imported from inside a
    zip itself. The staged copy contains `webcompanion/server.py` and
    `webcompanion/static/core.js` alongside everything else -- the daemon
    reads its static assets through `importlib.resources`, so they must be
    inside the archive, not merely on disk next to it.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "src"
        staged.mkdir()
        with as_file(files("webcompanion")) as pkg_dir:
            shutil.copytree(
                pkg_dir, staged / "webcompanion",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        if dest.exists():
            dest.unlink()
        zipapp.create_archive(
            staged, target=dest, interpreter="/usr/bin/env python3",
            main="webcompanion.cli:main",
        )
    return dest


class ConfigUnreadable(Exception):
    """A config file exists at `path` but could not be parsed.

    Deliberately distinct from "no config file": `config.load()` tolerates
    a corrupt file by returning bare defaults, which is right for a read
    path, but `ensure_config` minting a fresh token over it would silently
    discard the write token an IDE plugin is already using -- and every
    other setting -- dressing a disk error up as a first install.
    """

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path} exists but could not be read: {reason}")
        self.path = path
        self.reason = reason


def ensure_config() -> Config:
    """The config, minting a token only if there truly is none.

    Raises `ConfigUnreadable` -- and mints nothing, writes nothing -- if a
    config file is present but unparseable. Only a genuinely absent file is
    treated as "first install".
    """
    path = cfgmod.config_path()
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except OSError as e:
            raise ConfigUnreadable(path, str(e)) from e
        except json.JSONDecodeError as e:
            raise ConfigUnreadable(path, f"invalid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise ConfigUnreadable(path, "not a JSON object")

    cfg = cfgmod.load(path)
    if not cfg.token:
        cfg.token = cfgmod.mint_token()
        cfgmod.write(cfg, path)
    return cfg


def _load_and_restart(system: str, unit_path: Path, label: str) -> None:
    """Load (or reload) the service and restart it. This is the final step
    of install-service, so an upgrade takes effect immediately instead of
    leaving the old code running.

    Never called against a throwaway `target_dir` -- see `run()` and the
    task's HARD LIMIT on registering a real background service from here.
    """
    if system == "darwin":
        uid = os.getuid()
        domain = f"gui/{uid}"
        subprocess.run(["launchctl", "bootout", domain, str(unit_path)],
                        check=False, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(unit_path)],
                        check=False, capture_output=True)
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{label}"],
                        check=False, capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                        check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", label],
                        check=False, capture_output=True)
        subprocess.run(["systemctl", "--user", "restart", label],
                        check=False, capture_output=True)


def run(argv: list[str], *, target_dir: Path | None = None,
        label: str = DEFAULT_LABEL) -> int:
    """Build the zipapp, ensure the config, write the plist/unit, then load
    and restart the service.

    `target_dir` exists so tests (and only tests) can point the whole
    install path at a throwaway directory instead of the user's real login
    session -- see the task's HARD LIMIT. Left unset (the default used by
    the real CLI), this writes to the real launchd/systemd locations and
    actually loads the service.
    """
    try:
        ensure_config()
    except ConfigUnreadable as e:
        print(f"webcompanion: {e}\n"
              f"  fix or delete {e.path}, then re-run install-service",
              file=sys.stderr)
        return 1

    pyz = build_zipapp(default_zipapp_path())

    log_dir = paths.state_root()
    system = "darwin" if sys.platform == "darwin" else "linux"

    if system == "darwin":
        dest = (Path(target_dir) / f"{label}.plist") if target_dir is not None \
            else default_plist_path(label)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_plist(pyz=pyz, log_dir=log_dir, label=label))
    else:
        dest = (Path(target_dir) / DEFAULT_SERVICE_NAME) if target_dir is not None \
            else default_unit_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_unit(pyz=pyz, log_dir=log_dir))

    if target_dir is None:
        _load_and_restart(system, dest, label)
        print(f"webcompanion: installed {dest} and restarted {label}")
    else:
        print(f"webcompanion: wrote {dest} (throwaway install -- not loaded)")
    return 0
