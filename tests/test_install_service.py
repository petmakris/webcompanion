from __future__ import annotations

import json
import plistlib
import sys
import zipfile
from pathlib import Path

import pytest

from webcompanion import config as cfgmod
from webcompanion.commands import install_service as svc


def test_the_plist_runs_a_zipapp_with_the_system_python(tmp_path):
    xml = svc.render_plist(pyz=tmp_path / "webcompanion.pyz",
                           log_dir=tmp_path, label="dev.webcompanion")
    plist = plistlib.loads(xml.encode())
    args = plist["ProgramArguments"]
    assert args[0] == "/usr/bin/env"
    assert args[1] == "python3"
    assert args[2].endswith("webcompanion.pyz")
    assert args[3] == "serve"
    assert "site-packages" not in xml and "pipx" not in xml, (
        "a venv-bound interpreter dangles when its python is replaced")


def test_the_plist_sets_keepalive_a_throttle_and_a_log(tmp_path):
    plist = plistlib.loads(
        svc.render_plist(pyz=tmp_path / "w.pyz", log_dir=tmp_path,
                         label="dev.webcompanion").encode())
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] >= 10
    assert plist["StandardErrorPath"].endswith(".log")


def test_the_systemd_unit_restarts_always(tmp_path):
    unit = svc.render_unit(pyz=tmp_path / "w.pyz")
    assert "Restart=always" in unit
    assert "RestartSec=" in unit


def test_the_zipapp_is_self_contained_and_executable(tmp_path):
    pyz = svc.build_zipapp(tmp_path / "webcompanion.pyz")
    assert pyz.is_file()
    with zipfile.ZipFile(pyz) as z:
        names = z.namelist()
    assert "__main__.py" in names
    assert any(n.endswith("webcompanion/server.py") for n in names)
    assert any(n.endswith("webcompanion/static/core.js") for n in names), (
        "static assets must be inside the zipapp; the daemon reads them "
        "through importlib.resources")


def test_install_mints_a_token_only_when_there_is_not_one(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    cfgmod.write(cfgmod.Config(port=3080, token="keep-me"), p)
    svc.ensure_config()
    assert cfgmod.load(p).token == "keep-me", (
        "reminting on every install invalidates the IDE plugin's credential")


def test_install_mints_a_token_when_there_is_none(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    svc.ensure_config()
    assert len(cfgmod.load(p).token) >= 32


def test_install_service_never_touches_the_real_login_session_for_a_throwaway_target(
        tmp_path, monkeypatch):
    """The HARD LIMIT: exercising the whole install path against a throwaway
    target_dir/label must never call launchctl/systemctl. `_load_and_restart`
    is the only place those subprocess calls live; asserting it is not
    called (by making it raise if it is) is a stronger guarantee than trusting
    the label."""
    p = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)

    def _boom(*a, **k):
        raise AssertionError("must never load/restart a throwaway install")

    monkeypatch.setattr(svc, "_load_and_restart", _boom)
    monkeypatch.setattr(svc, "build_zipapp", lambda dest: dest)

    target = tmp_path / "throwaway-launchagents"
    rc = svc.run([], target_dir=target, label="throwaway.test.webcompanion")
    assert rc == 0
    written = list(target.iterdir())
    assert len(written) == 1
    expected = ("throwaway.test.webcompanion.plist" if sys.platform == "darwin"
                else "webcompanion.service")
    assert written[0].name == expected


def test_ensure_config_refuses_to_remint_over_a_corrupt_config(tmp_path, monkeypatch):
    """config.load() tolerates a corrupt file by returning bare defaults --
    right for a read path, but minting a fresh token here would silently
    discard the write token an IDE plugin is already using, plus every
    other setting, and dress a disk error up as a first install."""
    p = tmp_path / "config.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)

    with pytest.raises(svc.ConfigUnreadable):
        svc.ensure_config()

    assert p.read_text() == "{not valid json", "a corrupt file must not be overwritten"


def test_install_service_run_fails_loudly_on_a_corrupt_config_without_touching_it(
        tmp_path, monkeypatch, capsys):
    p = tmp_path / "config.json"
    original = json.dumps({"port": 3080, "token": "THE-PLUGIN-IS-USING-THIS",
                           "retention_days": 90})[:-1]  # truncated -> invalid JSON
    p.write_text(original)
    monkeypatch.setattr(cfgmod, "config_path", lambda: p)
    monkeypatch.setattr(svc, "_load_and_restart",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not reach load-and-restart")))

    rc = svc.run([], target_dir=tmp_path / "throwaway",
                  label="throwaway.corrupt.webcompanion")

    assert rc != 0
    assert p.read_text() == original, "a corrupt config must survive a failed install untouched"
    err = capsys.readouterr().err
    assert str(p) in err
    assert not (tmp_path / "throwaway").exists() or not any(
        (tmp_path / "throwaway").iterdir()), "must not write a plist/unit over a corrupt config"


# ── the service is addressed by the name that was written, and failure is
#    reported ─────────────────────────────────────────────────────────────
#
# Every test below fakes subprocess.run, so no real service is ever
# registered, unregistered, or restarted.

def _record_commands(monkeypatch, returncode=0, stderr=""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class Result:
            pass
        r = Result()
        r.returncode = returncode
        r.stdout = ""
        r.stderr = stderr
        return r

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    return calls


def test_the_systemd_calls_name_the_unit_file_that_was_written(tmp_path, monkeypatch):
    """install-service wrote `webcompanion.service` and then ran systemctl
    against `dev.webcompanion`, the launchd label. Every call named a unit
    that does not exist, so a Linux install started nothing."""
    calls = _record_commands(monkeypatch)
    unit_path = tmp_path / svc.DEFAULT_SERVICE_NAME

    problems = svc._load_and_restart("linux", unit_path, svc.DEFAULT_LABEL)

    assert problems == []
    named = [c[-1] for c in calls if c[-1] != "daemon-reload"]
    assert named and all(n == svc.DEFAULT_SERVICE_NAME for n in named), calls
    assert svc.DEFAULT_LABEL not in [tok for c in calls for tok in c]


def test_a_failed_systemctl_call_is_reported_not_swallowed(tmp_path, monkeypatch):
    """Every call was check=False, capture_output=True with the result
    discarded, so `installed ... and restarted` printed no matter what."""
    _record_commands(monkeypatch, returncode=5,
                     stderr="Failed to enable unit: does not exist")

    problems = svc._load_and_restart(
        "linux", tmp_path / svc.DEFAULT_SERVICE_NAME, svc.DEFAULT_LABEL)

    assert len(problems) == 3
    assert all("exited 5" in p for p in problems)
    assert any("does not exist" in p for p in problems)


def test_a_first_install_tolerates_only_the_launchd_bootout(tmp_path, monkeypatch):
    """`launchctl bootout` fails when nothing is loaded yet -- the normal
    first install. It is the one step allowed to fail; bootstrap and
    kickstart are not."""
    _record_commands(monkeypatch, returncode=1)

    problems = svc._load_and_restart(
        "darwin", tmp_path / "dev.webcompanion.plist", svc.DEFAULT_LABEL)

    assert len(problems) == 2
    assert not any("bootout" in p for p in problems)
    assert any("bootstrap" in p for p in problems)
    assert any("kickstart" in p for p in problems)


def test_run_returns_nonzero_and_says_so_when_the_service_would_not_start(
        tmp_path, monkeypatch, capsys):
    """The user-visible half: a failed start must not print success."""
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(svc, "build_zipapp", lambda dest: tmp_path / "w.pyz")
    monkeypatch.setattr(svc, "default_plist_path",
                        lambda label=svc.DEFAULT_LABEL: tmp_path / f"{label}.plist")
    monkeypatch.setattr(svc, "default_unit_path",
                        lambda name=svc.DEFAULT_SERVICE_NAME: tmp_path / name)
    monkeypatch.setattr(svc, "_load_and_restart",
                        lambda system, unit_path, label: ["`systemctl ...` exited 5"])

    assert svc.run([]) == 1
    err = capsys.readouterr().err
    assert "could not start" in err
    assert "exited 5" in err


# ── uninstall ────────────────────────────────────────────────────────────
#
# Every test below passes target_dir, so the real launchd/systemd session is
# never touched and no real service is unloaded.

from webcompanion.commands import uninstall as unins


def test_uninstall_removes_the_service_file_and_the_zipapp(tmp_path, monkeypatch, capsys):
    """`pipx uninstall webcompanion` leaves both behind: an always-on daemon
    holding port 3080 with no command left on the machine to stop it."""
    target = tmp_path / "agents"
    target.mkdir()
    name = ("dev.webcompanion.plist" if sys.platform == "darwin"
            else svc.DEFAULT_SERVICE_NAME)
    unit = target / name
    unit.write_text("<service definition>")
    # Inside `target`, because target_dir now scopes the zipapp too -- it
    # used to name only the plist/unit while the zipapp stayed at the real
    # ~/.local/share path, so this very test deleted a developer's installed
    # webcompanion.pyz.
    pyz = target / "webcompanion.pyz"
    pyz.write_text("zipapp")
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")

    assert unins.run([], target_dir=target) == 0
    assert not unit.exists()
    assert not pyz.exists()
    out = capsys.readouterr().out
    assert str(unit) in out and str(pyz) in out


def test_uninstall_leaves_the_config_and_the_workspaces_alone(tmp_path, monkeypatch, capsys):
    """The config carries the write token an IDE plugin has saved, and the
    workspaces are the user's data with no backup. Removing either is not a
    decision an uninstall command gets to make."""
    config = tmp_path / "config.json"
    cfgmod.write(cfgmod.Config(token="keep-me",
                               workspace_root=tmp_path / "ws"), config)
    monkeypatch.setattr(cfgmod, "config_path", lambda: config)
    workspace = tmp_path / "ws" / "annotate" / "250101-120000-aaaabbbbccccdddd"
    workspace.mkdir(parents=True)

    target = tmp_path / "agents"
    target.mkdir()
    assert unins.run([], target_dir=target) == 0

    assert config.is_file()
    assert json.loads(config.read_text())["token"] == "keep-me"
    assert workspace.is_dir()
    out = capsys.readouterr().out
    assert str(config) in out
    assert str(tmp_path / "ws") in out


def test_uninstalling_twice_is_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")
    target = tmp_path / "agents"
    target.mkdir()

    assert unins.run([], target_dir=target) == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_uninstall_is_a_registered_subcommand():
    from webcompanion import cli
    assert "uninstall" in cli.SUBCOMMANDS


def test_uninstall_never_shells_out_for_a_throwaway_target(tmp_path, monkeypatch):
    """The HARD LIMIT, asserted rather than assumed: with target_dir set,
    not one launchctl or systemctl call is made."""
    calls = []
    monkeypatch.setattr(unins.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)))
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")
    target = tmp_path / "agents"
    target.mkdir()

    unins.run([], target_dir=target)
    assert calls == []


def test_the_stop_step_names_the_unit_file_on_linux(tmp_path, monkeypatch):
    """Same trap install-service fell into: systemd addresses the unit by
    filename, not by the launchd label."""
    calls = []
    monkeypatch.setattr(unins.subprocess, "run",
                        lambda cmd, **kw: calls.append(list(cmd)))
    unins._stop("linux", tmp_path / svc.DEFAULT_SERVICE_NAME, svc.DEFAULT_LABEL)
    assert any(svc.DEFAULT_SERVICE_NAME in c for c in calls)
    assert not any(svc.DEFAULT_LABEL in c for c in calls)


def test_a_throwaway_uninstall_never_reaches_the_real_zipapp(tmp_path, monkeypatch,
                                                             capsys):
    """`target_dir` is what every test passes to stay off the real machine.
    It used to scope only the plist/unit; the zipapp path stayed at the real
    `~/.local/share/webcompanion/webcompanion.pyz` and was unlinked anyway.

    Records every path `uninstall` asks to remove instead of checking
    whether a file survived. A survival check passes vacuously on a machine
    where the real zipapp happens not to be installed -- which is most CI
    machines, and is exactly how this got shipped.
    """
    asked: list[Path] = []
    monkeypatch.setattr(unins, "_remove",
                        lambda path: asked.append(Path(path)) or False)
    monkeypatch.setattr(cfgmod, "config_path", lambda: tmp_path / "config.json")

    target = tmp_path / "agents"
    target.mkdir()
    assert unins.run([], target_dir=target) == 0

    assert asked, "uninstall considered nothing for removal"
    outside = [p for p in asked if target not in p.parents]
    assert outside == [], f"a throwaway uninstall reached outside {target}"
